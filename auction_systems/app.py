# app.py - FINAL, SECURED AND STABLE VERSION (PostgreSQL Compatible)

from flask import Flask, render_template, request, redirect, url_for, session, g
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
# सुरक्षा के लिए पासवर्ड हैशिंग लाइब्रेरी
from werkzeug.security import generate_password_hash, check_password_hash
import functools
import time
from threading import Timer
from datetime import datetime, timedelta
import io
import csv
from flask import Response
import os
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
# सीक्रेट की (इसे प्रोडक्शन में बदलें!)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'a_very_secure_random_string_for_development')
socketio = SocketIO(app)

# --- Database Setup ---

DATABASE = 'auction.db'
DATABASE_URL = os.getenv('DATABASE_URL')
# ऑक्शन के लिए टाइमर की अवधि
BID_DURATION = 15 # सेकंड में बिड की अवधि
NO_BID_DURATION = 120 # सेकंड में, अगर कोई बोली नहीं लगती है

# ऑक्शन ID के अनुसार एक्टिव बिड को ट्रैक करें
active_bids = {} 

def get_db_connection():
    """डेटाबेस कनेक्शन प्राप्त करें।"""
    if DATABASE_URL:
        conn = getattr(g, '_database_pg', None)
        if conn is None:
            conn = g._database_pg = psycopg2.connect(DATABASE_URL)
    else:
        conn = getattr(g, '_database', None)
        if conn is None:
            conn = g._database = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
    return conn

def get_dict_cursor(conn):
    """Get a cursor that returns rows as dictionaries."""
    if DATABASE_URL:
        return conn.cursor(cursor_factory=RealDictCursor)
    else:
        # For SQLite, we want the default row_factory to apply, 
        # so we just return a standard cursor.
        # The row_factory is set in get_db_connection for SQLite.
        return conn.cursor()

@app.teardown_appcontext
def close_connection(exception):
    """हर अनुरोध के बाद डेटाबेस कनेक्शन बंद करें।"""
    if DATABASE_URL:
        conn = getattr(g, '_database_pg', None)
    else:
        conn = getattr(g, '_database', None)
    if conn is not None:
        conn.close()

# --- init_db Function ---
def init_db():
    """डेटाबेस को इनिशियलाइज़ करें और टेबल बनाएं।"""
    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:
        # --- PostgreSQL Syntax ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                is_approved INTEGER NOT NULL DEFAULT 0,
                team_id INTEGER,
                can_bid INTEGER DEFAULT 0,
                discord_name TEXT,
                base_price REAL,
                game_level TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                budget REAL NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auctions (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                current_price REAL NOT NULL,
                highest_bidding_team_id INTEGER,
                status TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sold_players (
                id SERIAL PRIMARY KEY,
                player_name TEXT NOT NULL,
                winning_team_id INTEGER NOT NULL,
                sold_price REAL NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                message TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Add FOREIGN KEY constraints for PostgreSQL after tables are created
        try:
            cur.execute("ALTER TABLE users ADD CONSTRAINT fk_team_id FOREIGN KEY (team_id) REFERENCES teams (id)")
        except psycopg2.Error:
            pass # Constraint likely already exists
        try:
            cur.execute("ALTER TABLE auctions ADD CONSTRAINT fk_highest_bidding_team_id FOREIGN KEY (highest_bidding_team_id) REFERENCES teams (id)")
        except psycopg2.Error:
            pass # Constraint likely already exists
        try:
            cur.execute("ALTER TABLE sold_players ADD CONSTRAINT fk_winning_team_id FOREIGN KEY (winning_team_id) REFERENCES teams (id)")
        except psycopg2.Error:
            pass # Constraint likely already exists

    else:
        # --- SQLite Syntax ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL, -- 'admin', 'bidder'
                is_approved INTEGER NOT NULL DEFAULT 0,
                team_id INTEGER,
                can_bid INTEGER DEFAULT 0,
                discord_name TEXT,
                base_price REAL,
                game_level TEXT,
                FOREIGN KEY (team_id) REFERENCES teams (id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                budget REAL NOT NULL DEFAULT 0
            )
        """)
        # Check if 'budget' column exists in 'teams' table and add it if not
        cursor_pragma = conn.execute("PRAGMA table_info(teams)")
        columns = [col[1] for col in cursor_pragma.fetchall()]
        if 'budget' not in columns:
            cur.execute("ALTER TABLE teams ADD COLUMN budget REAL NOT NULL DEFAULT 0")
            print("Added 'budget' column to 'teams' table.") # For debugging
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auctions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                current_price REAL NOT NULL,
                highest_bidding_team_id INTEGER,
                status TEXT NOT NULL, -- 'live' or 'closed'
                FOREIGN KEY (highest_bidding_team_id) REFERENCES teams (id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sold_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT NOT NULL,
                winning_team_id INTEGER NOT NULL,
                sold_price REAL NOT NULL,
                FOREIGN KEY (winning_team_id) REFERENCES teams (id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
    
    conn.commit()
    cur.close()

# --- app_context Block ---
# एप्लिकेशन शुरू होने पर डेटाबेस और एडमिन उपयोगकर्ता को इनिशियलाइज़ करें
with app.app_context():
    init_db()
    # सुनिश्चित करें कि एक डिफ़ॉल्ट एडमिन यूज़र मौजूद है
    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:
        # --- PostgreSQL Syntax ---
        cur.execute("SELECT id FROM users WHERE username = %s", ('admin',))
        admin = cur.fetchone()
        if admin is None:
            hashed_password = generate_password_hash('adminpass')
            cur.execute(
                "INSERT INTO users (username, password, role, is_approved, team_id, can_bid) VALUES (%s, %s, %s, %s, %s, %s)",
                ('admin', hashed_password, 'admin', True, None, 0)
            )
        
        cur.execute("SELECT value FROM system_settings WHERE key = 'registration_open_until'")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO system_settings (key, value) VALUES (%s, %s)", ('registration_open_until', '0'))
        
        cur.execute("SELECT value FROM system_settings WHERE key = 'default_team_budget'")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO system_settings (key, value) VALUES (%s, %s)", ('default_team_budget', '100000.0'))
    else:
        # --- SQLite Syntax ---
        cur.execute("SELECT id FROM users WHERE username = ?", ('admin',))
        admin = cur.fetchone()
        if admin is None:
            hashed_password = generate_password_hash('adminpass')
            cur.execute(
                "INSERT INTO users (username, password, role, is_approved, team_id, can_bid) VALUES (?, ?, ?, ?, ?, ?)",
                ('admin', hashed_password, 'admin', True, None, 0)
            )

        cur.execute("SELECT value FROM system_settings WHERE key = 'registration_open_until'")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO system_settings (key, value) VALUES (?, ?)", ('registration_open_until', '0'))

        cur.execute("SELECT value FROM system_settings WHERE key = 'default_team_budget'")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO system_settings (key, value) VALUES (?, ?)", ('default_team_budget', '100000.0'))
            
    conn.commit()
    cur.close()


# --- User Authentication and Role Management ---

def is_admin():
    return session.get('role') == 'admin'

def get_current_user():
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    
    try:
        if DATABASE_URL:
            cur.execute("SELECT * FROM users WHERE username = %s", (session.get('username'),))
        else:
            cur.execute("SELECT * FROM users WHERE username = ?", (session.get('username'),))
        user = cur.fetchone()
    except Exception as e:
        print(f"Error in get_current_user: {e}")
        user = None
    finally:
        cur.close()
        
    return user

def is_approved_bidder():
    user = get_current_user()
    if not user:
        return False
        
    if DATABASE_URL:
        # PostgreSQL with RealDictCursor returns dicts
        return session.get('role') == 'bidder' and user and user['is_approved']
    else:
        # SQLite with row_factory returns Row objects (which act like dicts)
        return session.get('role') == 'bidder' and user and user['is_approved']


# --- [FIXED] broadcast_stats Function ---
def broadcast_stats():
    """Calculates and broadcasts auction stats to all clients."""
    with app.app_context():
        conn = get_db_connection() 
        cur = get_dict_cursor(conn)
        
        if DATABASE_URL:
            cur.execute("SELECT COUNT(id) AS count FROM users WHERE role = 'bidder' AND base_price IS NOT NULL")
            total_players_result = cur.fetchone()
            total_players = total_players_result['count'] if total_players_result and 'count' in total_players_result else 0
            
            cur.execute("SELECT COUNT(id) AS count FROM sold_players")
            sold_result = cur.fetchone()
            sold_players_count = sold_result['count'] if sold_result and 'count' in sold_result else 0
            
            cur.execute("SELECT COUNT(id) AS count FROM auctions WHERE status = 'Unsold'")
            unsold_result = cur.fetchone()
            unsold_auctions_count = unsold_result['count'] if unsold_result and 'count' in unsold_result else 0
        else:
            cur.execute("SELECT COUNT(id) AS count FROM users WHERE role = 'bidder' AND base_price IS NOT NULL")
            total_players_result = cur.fetchone()
            total_players = total_players_result['count'] if total_players_result and 'count' in total_players_result else 0
            
            cur.execute("SELECT COUNT(id) AS count FROM sold_players")
            sold_result = cur.fetchone()
            sold_players_count = sold_result['count'] if sold_result and 'count' in sold_result else 0
            
            cur.execute("SELECT COUNT(id) AS count FROM auctions WHERE status = 'Unsold'")
            unsold_result = cur.fetchone()
            unsold_auctions_count = unsold_result['count'] if unsold_result and 'count' in unsold_result else 0
            
        cur.close()
        unsold_players = unsold_auctions_count # Simplified logic
        stats = {'total_players': total_players, 'sold_players': sold_players_count, 'unsold_players': unsold_players}
        socketio.emit('stats_update', stats)

def log_activity(message):
    """Broadcasts a generic activity message to all clients."""
    with app.app_context(): # Added app_context for thread safety
        conn = get_db_connection()
        cur = get_dict_cursor(conn)
        timestamp = time.strftime('%H:%M:%S')
        
        try:
            if DATABASE_URL:
                cur.execute("INSERT INTO activity_log (message, timestamp) VALUES (%s, %s)", (message, timestamp))
            else:
                cur.execute("INSERT INTO activity_log (message, timestamp) VALUES (?, ?)", (message, timestamp))
            conn.commit()
        except Exception as e:
            print(f"Error logging activity: {e}")
            conn.rollback() # Rollback on error
        finally:
            cur.close()

        activity_data = { 
            'message': message,
            'timestamp': time.strftime('%H:%M:%S')
        }
        socketio.emit('new_activity', activity_data)


# --- Routes ---

# --- [FIXED] index Route ---
@app.route('/')
def index():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for('login'))

    if is_admin(): # If admin, redirect to admin dashboard
        return redirect(url_for('admin_dashboard'))
    
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    
    # यूज़र की टीम का नाम प्राप्त करें
    team_name = "Not Assigned"
    team_budget = 0
    if user['team_id']:
        if DATABASE_URL:
            cur.execute("SELECT name, budget FROM teams WHERE id = %s", (user['team_id'],))
        else:
            cur.execute("SELECT name, budget FROM teams WHERE id = ?", (user['team_id'],))
        team = cur.fetchone() 
        if team:
            team_name = team['name']
            team_budget = team['budget']

    if DATABASE_URL:
        cur.execute("""
            SELECT t.name as team_name, sp.player_name
            FROM sold_players sp
            JOIN teams t ON sp.winning_team_id = t.id
            ORDER BY t.name, sp.player_name
        """)
    else:
        cur.execute("""
            SELECT t.name as team_name, sp.player_name
            FROM sold_players sp
            JOIN teams t ON sp.winning_team_id = t.id
            ORDER BY t.name, sp.player_name
        """)
    sold_players_by_team_list = cur.fetchall()
    
    sold_players_by_team = {}
    for player in sold_players_by_team_list:
        if player['team_name'] not in sold_players_by_team:
            sold_players_by_team[player['team_name']] = []
        sold_players_by_team[player['team_name']].append(player['player_name'])

    if DATABASE_URL:
        cur.execute("""
            SELECT a.id, a.title, a.current_price, a.status, t.name as highest_bidder_username FROM auctions a
            LEFT JOIN teams t ON a.highest_bidding_team_id = t.id
            WHERE a.status = 'live'
        """)
    else:
        cur.execute("""
            SELECT a.id, a.title, a.current_price, a.status, t.name as highest_bidder_username FROM auctions a
            LEFT JOIN teams t ON a.highest_bidding_team_id = t.id
            WHERE a.status = 'live'
        """)
    auctions = cur.fetchall()
    
    cur.execute("SELECT name FROM teams")
    all_teams = cur.fetchall()

    if DATABASE_URL:
        cur.execute("SELECT COUNT(id) AS count FROM users WHERE role = 'bidder' AND base_price IS NOT NULL")
        total_players_result = cur.fetchone()
        total_players = total_players_result['count'] if total_players_result and 'count' in total_players_result else 0
        
        cur.execute("SELECT COUNT(id) AS count FROM sold_players")
        sold_result = cur.fetchone()
        sold_players = sold_result['count'] if sold_result and 'count' in sold_result else 0
        
        cur.execute("SELECT COUNT(id) AS count FROM auctions WHERE status = 'Unsold'")
        unsold_result = cur.fetchone()
        unsold_players_count = unsold_result['count'] if unsold_result and 'count' in unsold_result else 0
    else:
        cur.execute("SELECT COUNT(id) AS count FROM users WHERE role = 'bidder' AND base_price IS NOT NULL")
        total_players_result = cur.fetchone()
        total_players = total_players_result['count'] if total_players_result and 'count' in total_players_result else 0
        
        cur.execute("SELECT COUNT(id) AS count FROM sold_players")
        sold_result = cur.fetchone()
        sold_players = sold_result['count'] if sold_result and 'count' in sold_result else 0
        
        cur.execute("SELECT COUNT(id) AS count FROM auctions WHERE status = 'Unsold'")
        unsold_result = cur.fetchone()
        unsold_players_count = unsold_result['count'] if unsold_result and 'count' in unsold_result else 0
        
    cur.close()
    
    unsold_players = unsold_players_count

    return render_template('auction_feed.html', 
                           auctions=auctions, 
                           current_username=session.get('username'),
                           team_name=team_name,
                           team_budget=team_budget,
                           can_bid=user['can_bid'],
                           all_teams=all_teams,
                           sold_players_by_team=sold_players_by_team,
                           total_players = total_players,
                           sold_players=sold_players,
                           unsold_players=unsold_players)

@app.route('/register', methods=('GET', 'POST'))
def register():
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    cur.execute("SELECT value FROM system_settings WHERE key = 'registration_open_until'")
    setting = cur.fetchone()

    registration_open = False
    open_until_timestamp = 0.0
    if setting:
        # Handle potential string or dict access
        setting_value = setting['value'] if isinstance(setting, dict) else setting[0]
        open_until_timestamp = float(setting_value)
        if time.time() < open_until_timestamp:
            registration_open = True

    if not registration_open:
        cur.close()
        return render_template('register.html', 
                               error="Player registration is currently closed. Please check back later.", 
                               registration_closed=True)

    if request.method == 'POST':
        username = request.form['username']
        password_hash = generate_password_hash(request.form['password'])
        discord_name = request.form['discord_name']
        base_price = float(request.form['base_price'])
        game_level = request.form['game_level']

        try:
            if DATABASE_URL:
                cur.execute("INSERT INTO users (username, password, role, is_approved, discord_name, base_price, game_level) VALUES (%s, %s, %s, %s, %s, %s, %s)", (username, password_hash, 'bidder', True, discord_name, base_price, game_level))
            else:
                cur.execute("INSERT INTO users (username, password, role, is_approved, discord_name, base_price, game_level) VALUES (?, ?, ?, ?, ?, ?, ?)", (username, password_hash, 'bidder', 1, discord_name, base_price, game_level))

            conn.commit()
            cur.close()
            # Broadcast updated stats
            broadcast_stats()
            log_activity(f"New player '{username}' has registered.")
            return redirect(url_for('login', message="Registration successful! You can now log in."))

        except (sqlite3.IntegrityError, psycopg2.IntegrityError):
            cur.close()
            return render_template('register.html', error="Username already exists.", registration_open_until=open_until_timestamp)
        except Exception as e:
            cur.close()
            return render_template('register.html', error=f"An error occurred: {e}", registration_open_until=open_until_timestamp)

    cur.close()
    return render_template('register.html', registration_open_until=open_until_timestamp)

@app.route('/register_team', methods=['GET', 'POST'])
def register_team():
    if not is_admin():
        return redirect(url_for('index'))
    if request.method == 'POST':
        team_name = request.form['team_name'].strip()
        password = request.form['password']
        if not team_name or not password:
            return redirect(url_for('manage_teams', error="Team name and password cannot be empty."))
        
        conn = get_db_connection()
        cur = get_dict_cursor(conn)
        
        try:
            cur.execute("SELECT value FROM system_settings WHERE key = 'default_team_budget'")
            default_budget_setting = cur.fetchone()
            default_budget = float(default_budget_setting['value']) if default_budget_setting else 100000.0

            if DATABASE_URL:
                cur.execute("INSERT INTO teams (name, budget) VALUES (%s, %s) RETURNING id", (team_name, float(default_budget)))
                team_id = cur.fetchone()['id']
            else:
                cur.execute("INSERT INTO teams (name, budget) VALUES (?, ?)", (team_name, float(default_budget)))
                team_id = cur.lastrowid
            
            password_hash = generate_password_hash(password)
            
            if DATABASE_URL:
                cur.execute("INSERT INTO users (username, password, role, is_approved, team_id, can_bid) VALUES (%s, %s, %s, %s, %s, %s)", (team_name, password_hash, 'bidder', True, team_id, True))
            else:
                cur.execute("INSERT INTO users (username, password, role, is_approved, team_id, can_bid) VALUES (?, ?, ?, ?, ?, ?)", (team_name, password_hash, 'bidder', 1, team_id, 1))

            conn.commit()
            cur.close()
            
            team_data = {'name': team_name, 'budget': float(default_budget)}
            socketio.emit('new_team_added', team_data)
            log_activity(f"A new team has been created: '{team_name}'.")
            return redirect(url_for('manage_teams', success=f"Team '{team_name}' created successfully."))
        
        except (sqlite3.IntegrityError, psycopg2.IntegrityError):
            conn.rollback()
            cur.close()
            return redirect(url_for('manage_teams', error=f"Team name '{team_name}' already exists."))
        except Exception as e:
            conn.rollback()
            cur.close()
            return redirect(url_for('manage_teams', error=f"An error occurred: {e}"))
            
    return redirect(url_for('manage_teams'))

@app.route('/login_player', methods=('GET', 'POST'))
def login_player(): # This is the player login route 
    message = request.args.get('message')
    error = None # Initialize error
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        cur = get_dict_cursor(conn)
        
        if DATABASE_URL:
            cur.execute("SELECT * FROM users WHERE username = %s AND (role = 'admin' OR base_price IS NOT NULL)", (username,))
        else:
            cur.execute("SELECT * FROM users WHERE username = ? AND (role = 'admin' OR base_price IS NOT NULL)", (username,))
        user = cur.fetchone()
        cur.close()
        
        if user and check_password_hash(user['password'], password):
            session['username'] = user['username']
            session['role'] = user['role']
            if user['role'] == 'bidder' and user['team_id']: # Store team_id for players if assigned
                session['team_id'] = user['team_id']
            return redirect(url_for('index'))
        else:
            message = None # Clear message if login fails
            error = "Invalid username or password."
            
    return render_template('login_player.html', error=error, message=message)


@app.route('/login', methods=('GET', 'POST'))
def login():
    # This route will now redirect to the player login page by default.
    return redirect(url_for('login_player'))

@app.route('/login_team', methods=('GET', 'POST'))
def login_team():
    message = request.args.get('message') 
    error = None
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        cur = get_dict_cursor(conn)
        
        if DATABASE_URL:
            cur.execute("SELECT * FROM users WHERE username = %s AND role = 'bidder' AND base_price IS NULL", (username,))
        else:
            cur.execute("SELECT * FROM users WHERE username = ? AND role = 'bidder' AND base_price IS NULL", (username,))
        user = cur.fetchone()
        cur.close()
        
        if user and check_password_hash(user['password'], password):
            session['username'] = user['username']
            session['role'] = user['role']
            session['team_id'] = user['team_id']
            return redirect(url_for('index'))
        else:
            error = "Invalid team username or password."
            message = None # Clear message
            
    return render_template('login_team.html', error=error, message=message)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login')) # Redirect to the main login page


# --- Admin Routes ---

# --- [FIXED] admin_dashboard Route ---
@app.route('/admin')
def admin_dashboard():
    if not is_admin():
        return redirect(url_for('index'))

    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    
    if DATABASE_URL:
        cur.execute("""
            SELECT a.id, a.title, a.current_price, a.status, t.name as highest_bidder_username
            FROM auctions a
            LEFT JOIN teams t ON a.highest_bidding_team_id = t.id
            ORDER BY a.id DESC
        """)
    else:
        cur.execute("""
            SELECT a.id, a.title, a.current_price, a.status, t.name as highest_bidder_username
            FROM auctions a
            LEFT JOIN teams t ON a.highest_bidding_team_id = t.id
            ORDER BY a.id DESC
        """)
    all_auctions = cur.fetchall()
    
    cur.execute("SELECT id, name FROM teams")
    teams = cur.fetchall()

    if DATABASE_URL:
        cur.execute("""
            SELECT u.id, u.username, u.discord_name, u.base_price, u.game_level
            FROM users u LEFT JOIN auctions a ON u.username = a.title
            WHERE u.role = 'bidder' AND a.id IS NULL AND u.base_price IS NOT NULL
            ORDER BY u.id DESC
        """)
    else:
        cur.execute("""
            SELECT u.id, u.username, u.discord_name, u.base_price, u.game_level
            FROM users u LEFT JOIN auctions a ON u.username = a.title
            WHERE u.role = 'bidder' AND a.id IS NULL AND u.base_price IS NOT NULL
            ORDER BY u.id DESC
        """)
    players_ready_for_auction = cur.fetchall()

    # --- START FIX: Robust fetching for stats ---
    if DATABASE_URL:
        cur.execute("SELECT COUNT(id) AS count FROM users WHERE role = 'bidder' AND base_price IS NOT NULL")
        total_players_result = cur.fetchone()
        total_players = total_players_result['count'] if total_players_result and 'count' in total_players_result else 0
        
        cur.execute("SELECT COUNT(id) AS count FROM sold_players")
        sold_result = cur.fetchone()
        sold_players = sold_result['count'] if sold_result and 'count' in sold_result else 0
    else:
        cur.execute("SELECT COUNT(id) AS count FROM users WHERE role = 'bidder' AND base_price IS NOT NULL")
        total_players_result = cur.fetchone()
        total_players = total_players_result['count'] if total_players_result and 'count' in total_players_result else 0
        
        cur.execute("SELECT COUNT(id) AS count FROM sold_players")
        sold_result = cur.fetchone()
        sold_players = sold_result['count'] if sold_result and 'count' in sold_result else 0

    # --- END FIX: Robust fetching for stats ---
    
    unsold_players = total_players - sold_players
    
    if DATABASE_URL:
        cur.execute("""
            SELECT a.id, a.title, u.discord_name, u.base_price, u.game_level
            FROM auctions a
            JOIN users u ON a.title = u.username
            WHERE a.status = 'Unsold'
        """)
    else:
        cur.execute("""
            SELECT a.id, a.title, u.discord_name, u.base_price, u.game_level
            FROM auctions a
            JOIN users u ON a.title = u.username
            WHERE a.status = 'Unsold'
        """)
    unsold_auctions = cur.fetchall()

    # Get registration status
    cur.execute("SELECT value FROM system_settings WHERE key = 'registration_open_until'")
    setting = cur.fetchone()
    registration_status = 'closed'
    registration_ends_at = None
    if setting:
        # Handling for both RealDictCursor (dict) and standard cursor (Row/tuple access)
        setting_value = setting['value'] if isinstance(setting, dict) and 'value' in setting else setting[0] if isinstance(setting, tuple) and len(setting) > 0 else '0'
        open_until_timestamp = float(setting_value)
        if time.time() < open_until_timestamp:
            registration_status = 'open'
            ends_dt = datetime.fromtimestamp(open_until_timestamp)
            registration_ends_at = ends_dt.strftime('%Y-%m-%d %H:%M:%S')

    registration_status_display = "Open" if registration_status == 'open' else "Closed"

    # Get default budget
    cur.execute("SELECT value FROM system_settings WHERE key = 'default_team_budget'")
    default_budget_setting = cur.fetchone()
    default_team_budget = float(default_budget_setting['value']) if default_budget_setting else 0.0

    cur.execute("SELECT id, name, budget FROM teams ORDER BY name")
    teams_with_budgets = cur.fetchall()
    cur.close()


    return render_template('admin_dashboard.html', 
                           auctions=all_auctions,
                           teams=teams,
                           players_ready_for_auction=players_ready_for_auction,
                           total_players=total_players,
                           teams_with_budgets=teams_with_budgets,
                           default_team_budget=default_team_budget,
                           sold_players=sold_players,
                           unsold_players=unsold_players, # This is a count
                           unsold_auctions=unsold_auctions, # This is the list of auctions
                           registration_status=registration_status,
                           registration_status_display=registration_status_display,
                           registration_ends_at=registration_ends_at)

def mark_as_unsold(auction_id):
    """यदि कोई बोली नहीं लगाई जाती है तो नीलामी को 'Unsold' के रूप में चिह्नित करता है।"""
    with app.app_context():
        conn = get_db_connection()
        cur = get_dict_cursor(conn)
        
        try:
            if DATABASE_URL:
                cur.execute("SELECT * FROM auctions WHERE id = %s AND status = 'live'", (auction_id,))
            else:
                cur.execute("SELECT * FROM auctions WHERE id = ? AND status = 'live'", (auction_id,))
            auction = cur.fetchone()
            
            if auction and auction['highest_bidding_team_id'] is None:
                if DATABASE_URL:
                    cur.execute("UPDATE auctions SET status = 'Unsold' WHERE id = %s", (auction_id,))
                else:
                    cur.execute("UPDATE auctions SET status = 'Unsold' WHERE id = ?", (auction_id,))
                conn.commit()
                
                player_name = auction['title']
                log_activity(f"Player '{player_name}' went unsold as no bids were placed.")
                socketio.emit('player_unsold', {'auction_id': auction_id, 'player_name': player_name})
                broadcast_stats()
        except Exception as e:
            print(f"Error in mark_as_unsold: {e}")
            conn.rollback()
        finally:
            cur.close()

@app.route('/admin/toggle_registration', methods=['POST'])
def toggle_registration():
    if not is_admin():
        return redirect(url_for('index'))

    action = request.form.get('action')
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        if action == 'open':
            # Set registration to be open for the next 24 hours
            open_until = time.time() + (24 * 60 * 60)
            if DATABASE_URL:
                cur.execute("UPDATE system_settings SET value = %s WHERE key = 'registration_open_until'", (str(open_until),))
            else:
                cur.execute("UPDATE system_settings SET value = ? WHERE key = 'registration_open_until'", (str(open_until),))
            log_activity("Admin has opened player registration for 24 hours.")
        elif action == 'close':
            # Close registration immediately
            if DATABASE_URL:
                cur.execute("UPDATE system_settings SET value = '0' WHERE key = 'registration_open_until'")
            else:
                cur.execute("UPDATE system_settings SET value = '0' WHERE key = 'registration_open_until'")
            log_activity("Admin has closed player registration.")
        
        conn.commit()
    except Exception as e:
        print(f"Error toggling registration: {e}")
        conn.rollback()
    finally:
        cur.close()
      
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_budget', methods=['POST'])
def update_budget():
    if not is_admin():
        return redirect(url_for('index'))

    new_budget = request.form.get('default_budget')
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        if DATABASE_URL:
            cur.execute("UPDATE system_settings SET value = %s WHERE key = 'default_team_budget'", (new_budget,))
        else:
            cur.execute("UPDATE system_settings SET value = ? WHERE key = 'default_team_budget'", (new_budget,))
        conn.commit()
        log_activity(f"Admin updated default team budget to {new_budget}.")
    except Exception as e:
        print(f"Error updating budget: {e}")
        conn.rollback()
    finally:
        cur.close()
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_team_budget', methods=['POST'])
def update_team_budget():
    if not is_admin():
        return redirect(url_for('index'))
    team_id = request.form.get('team_id')
    new_budget = request.form.get('new_budget')
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    try:
        if DATABASE_URL:
            cur.execute("UPDATE teams SET budget = %s WHERE id = %s", (new_budget, team_id))
        else:
            cur.execute("UPDATE teams SET budget = ? WHERE id = ?", (new_budget, team_id))
        conn.commit()
        log_activity(f"Admin updated team id '{team_id}' budget to {new_budget}.")
    except Exception as e:
        print(f"Error updating team budget: {e}")
        conn.rollback()
    finally:
        cur.close()
    return redirect(url_for('manage_teams'))

@app.route('/admin/reauction/<int:auction_id>', methods=['POST'])
def reauction_player(auction_id):
    """एक बिना बिके खिलाड़ी को फिर से नीलाम करता है।"""
    if not is_admin():
        return redirect(url_for('index'))
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    try:
        if DATABASE_URL:
            cur.execute("SELECT * FROM auctions WHERE id = %s AND status = 'Unsold'", (auction_id,))
        else:
            cur.execute("SELECT * FROM auctions WHERE id = ? AND status = 'Unsold'", (auction_id,))
        auction = cur.fetchone()
        if not auction:
            cur.close()
            return "Unsold auction not found", 404
        if DATABASE_URL:
            cur.execute("SELECT * FROM users WHERE username = %s", (auction['title'],))
        else:
            cur.execute("SELECT * FROM users WHERE username = ?", (auction['title'],))
        player = cur.fetchone()
        if not player:
            cur.close()
            return "Player for this auction not found", 404
        
        # नीलामी को रीसेट करें
        if DATABASE_URL:
            cur.execute("UPDATE auctions SET status = 'live', current_price = %s, highest_bidding_team_id = NULL WHERE id = %s", (player['base_price'], auction_id))
        else:
            cur.execute("UPDATE auctions SET status = 'live', current_price = ?, highest_bidding_team_id = NULL WHERE id = ?", (player['base_price'], auction_id))
        conn.commit()
        cur.close()
        
        # Re-emit the new_auction event to make it appear on all feeds
        socketio.emit('new_auction', {
            'id': auction_id,
            'title': auction['title'],
            'price': player['base_price'],
            'discord_name': player['discord_name'],
            'base_price': player['base_price'],
            'game_level': player['game_level'],
            'time_left': NO_BID_DURATION
        })
        log_activity(f"Player '{auction['title']}' is being re-auctioned.")
        
        # 60-सेकंड का 'नो-बिड' टाइमर फिर से शुरू करें
        timer_thread = Timer(NO_BID_DURATION, mark_as_unsold, args=[auction_id])
        timer_thread.start()
        # Store the timer thread and its end time
        end_time = time.time() + NO_BID_DURATION
        active_bids[auction_id] = {'thread': timer_thread, 'end_time': end_time}
        
    except Exception as e:
        print(f"Error re-auctioning player: {e}")
        conn.rollback()
        cur.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/start_auction/<int:user_id>', methods=['POST'])
def start_auction(user_id):
    """एक खिलाड़ी के लिए नीलामी शुरू करता है जो अभी तक नीलाम नहीं हुआ है।"""
    if not is_admin():
        return redirect(url_for('index'))
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    try:
        if DATABASE_URL:
            cur.execute("SELECT * FROM users WHERE id = %s AND role = 'bidder'", (user_id,))
        else:
            cur.execute("SELECT * FROM users WHERE id = ? AND role = 'bidder'", (user_id,))
        player = cur.fetchone()
        if not player:
            cur.close()
            return "Player not found", 404
        
        if DATABASE_URL:
            cur.execute("SELECT id FROM auctions WHERE title = %s", (player['username'],))
        else:
            cur.execute("SELECT id FROM auctions WHERE title = ?", (player['username'],))
        existing_auction = cur.fetchone()
        if existing_auction:
            cur.close()
            return redirect(url_for('admin_dashboard', error=f"Auction for {player['username']} already exists."))

        # खिलाड़ी के लिए एक नई नीलामी बनाएँ
        if DATABASE_URL:
            cur.execute("INSERT INTO auctions (title, current_price, status) VALUES (%s, %s, %s) RETURNING id", (player['username'], player['base_price'], 'live'))
            auction_id = cur.fetchone()['id']
        else:
            cur.execute("INSERT INTO auctions (title, current_price, status) VALUES (?, ?, ?)", (player['username'], player['base_price'], 'live'))
            auction_id = cur.lastrowid
        conn.commit()
        
        # सभी को नई नीलामी के बारे में सूचित करें
        socketio.emit('new_auction', {
            'id': auction_id,
            'title': player['username'],
            'price': player['base_price'],
            'discord_name': player['discord_name'],
            'base_price': player['base_price'],
            'game_level': player['game_level'],
            'time_left': NO_BID_DURATION
        })
        cur.close()
        log_activity(f"Auction started for player '{player['username']}' with a base price of ₹{player['base_price']:.2f}.")

        # 60-सेकंड का 'नो-बिड' टाइमर शुरू करें
        timer_thread = Timer(NO_BID_DURATION, mark_as_unsold, args=[auction_id])
        timer_thread.start()
        end_time = time.time() + NO_BID_DURATION
        active_bids[auction_id] = {'thread': timer_thread, 'end_time': end_time}
    except Exception as e:
        print(f"Error starting auction: {e}")
        conn.rollback()
        cur.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add_auction', methods=['POST'])
def add_auction():
    if not is_admin():
        return redirect(url_for('index'))
    title = request.form['title']
    starting_price = float(request.form['price'])
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    try:
        if DATABASE_URL:
            cur.execute("INSERT INTO auctions (title, current_price, status) VALUES (%s, %s, %s)", (title, starting_price, 'live'))
        else:
            cur.execute("INSERT INTO auctions (title, current_price, status) VALUES (?, ?, ?)", (title, starting_price, 'live'))
        conn.commit()
        cur.close()
        socketio.emit('new_auction', {'title': title, 'price': starting_price})
    except Exception as e:
        print(f"Error adding auction: {e}")
        conn.rollback()
        cur.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/manage_teams')
def manage_teams():
    if not is_admin():
        return redirect(url_for('index'))
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    
    if DATABASE_URL:
        # PostgreSQL syntax for aggregate function
        cur.execute("""
            SELECT t.id, t.name, t.budget, STRING_AGG(sp.player_name, ', ') as members
            FROM teams t
            LEFT JOIN sold_players sp ON sp.winning_team_id = t.id
            GROUP BY t.id, t.name, t.budget
            ORDER BY t.name
        """)
    else:
        # SQLite syntax for aggregate function
        cur.execute("""
            SELECT 
                t.id, 
                t.name, 
                t.budget, 
                (
                    SELECT GROUP_CONCAT(sp.player_name, ', ') 
                    FROM sold_players sp 
                    WHERE sp.winning_team_id = t.id
                ) as members
            FROM teams t
            ORDER BY t.name
        """)
    teams_with_budgets = cur.fetchall()
    
    # Get sold players by team for the display below
    if DATABASE_URL:
        cur.execute("""
            SELECT t.name as team_name, sp.player_name
            FROM sold_players sp
            JOIN teams t ON sp.winning_team_id = t.id
            ORDER BY t.name, sp.player_name
        """)
    else:
        cur.execute("""
            SELECT t.name as team_name, sp.player_name
            FROM sold_players sp
            JOIN teams t ON sp.winning_team_id = t.id
            ORDER BY t.name, sp.player_name
        """)
    sold_players_by_team = cur.fetchall()
    
    cur.execute("SELECT id, name FROM teams ORDER BY name")
    all_teams = cur.fetchall()
    cur.close()
    
    return render_template('manage_teams.html', 
                           teams_with_budgets=teams_with_budgets,
                           all_teams=all_teams,
                           sold_players=sold_players_by_team,
                           error=request.args.get('error'),
                           success=request.args.get('success')) # Added success message

@app.route('/download_sold_players')
def download_sold_players():
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    if DATABASE_URL:
        cur.execute("""
            SELECT sp.player_name, t.name as team_name, sp.sold_price
            FROM sold_players sp
            JOIN teams t ON sp.winning_team_id = t.id
            ORDER BY t.name, sp.player_name
        """)
    else:
        cur.execute("""
            SELECT sp.player_name, t.name as team_name, sp.sold_price
            FROM sold_players sp
            JOIN teams t ON sp.winning_team_id = t.id
            ORDER BY t.name, sp.player_name
        """)
    sold_players_data = cur.fetchall()
    cur.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Player Name', 'Team Name', 'Sold Price (₹)'])
    for player in sold_players_data:
        writer.writerow([player['player_name'], player['team_name'], player['sold_price']])
    
    output.seek(0)
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=sold_players.csv"}
    )

@app.route('/download_team_roster')
def download_team_roster():
    conn = get_db_connection()
    cur = get_dict_cursor(conn)
    
    if DATABASE_URL:
        cur.execute("""
            SELECT t.name, t.budget, STRING_AGG(sp.player_name, ', ') as members
            FROM teams t
            LEFT JOIN sold_players sp ON sp.winning_team_id = t.id
            GROUP BY t.id, t.name, t.budget
            ORDER BY t.name
        """)
    else:
        # SQLite
        cur.execute("""
            SELECT 
                t.name, 
                t.budget, 
                (
                    SELECT GROUP_CONCAT(sp.player_name, ', ') 
                    FROM sold_players sp 
                    WHERE sp.winning_team_id = t.id
                ) as members
            FROM teams t
            ORDER BY t.name
        """)
    teams_data = cur.fetchall()
    cur.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Team Name', 'Budget Remaining (₹)', 'Players Bought'])
    for team in teams_data:
        writer.writerow([team['name'], team['budget'], team['members'] or ''])
    
    output.seek(0)
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=team_roster.csv"}
    )

# --- SocketIO for Live Bidding ---

def end_bidding(auction_id):
    """बिडिंग अवधि समाप्त होने पर कॉल किया जाने वाला फ़ंक्शन।"""
    with app.app_context():
        print(f"Auction {auction_id} bidding period ended. Determining winner.")
        conn = get_db_connection()
        cur = get_dict_cursor(conn)
        
        try:
            if DATABASE_URL:
                cur.execute("SELECT * FROM auctions WHERE id = %s AND status = 'live'", (auction_id,))
            else:
                cur.execute("SELECT * FROM auctions WHERE id = ? AND status = 'live'", (auction_id,))
            auction = cur.fetchone()
            
            if not auction:
                print(f"Auction {auction_id} not found or already closed/sold.")
                return
            
            player_name = auction['title']
            winning_team_id = auction['highest_bidding_team_id']
            sold_price = auction['current_price']
            
            if winning_team_id is None:
                # Unsold logic
                if DATABASE_URL:
                    cur.execute("UPDATE auctions SET status = 'Unsold' WHERE id = %s", (auction_id,))
                else:
                    cur.execute("UPDATE auctions SET status = 'Unsold' WHERE id = ?", (auction_id,))
                conn.commit()
                
                log_activity(f"Player '{player_name}' went unsold as the timer ran out.")
                socketio.emit('player_unsold', {'auction_id': auction_id, 'player_name': player_name})
                broadcast_stats()
                
            else:
                # Sold logic
                if DATABASE_URL:
                    cur.execute("UPDATE auctions SET status = 'Sold' WHERE id = %s", (auction_id,))
                    cur.execute("INSERT INTO sold_players (player_name, winning_team_id, sold_price) VALUES (%s, %s, %s)", (player_name, winning_team_id, sold_price))
                    cur.execute("SELECT id FROM users WHERE username = %s", (player_name,))
                else:
                    cur.execute("UPDATE auctions SET status = 'Sold' WHERE id = ?", (auction_id,))
                    cur.execute("INSERT INTO sold_players (player_name, winning_team_id, sold_price) VALUES (?, ?, ?)", (player_name, winning_team_id, sold_price))
                    cur.execute("SELECT id FROM users WHERE username = ?", (player_name,))
                
                player_user = cur.fetchone()
                if player_user:
                    if DATABASE_URL:
                        cur.execute("UPDATE users SET team_id = %s WHERE id = %s", (winning_team_id, player_user['id']))
                    else:
                        cur.execute("UPDATE users SET team_id = ? WHERE id = ?", (winning_team_id, player_user['id']))
                        
                if DATABASE_URL:
                    cur.execute("UPDATE teams SET budget = budget - %s WHERE id = %s", (sold_price, winning_team_id))
                    cur.execute("SELECT name, budget FROM teams WHERE id = %s", (winning_team_id,))
                else:
                    cur.execute("UPDATE teams SET budget = budget - ? WHERE id = ?", (sold_price, winning_team_id))
                    cur.execute("SELECT name, budget FROM teams WHERE id = ?", (winning_team_id,))
                    
                conn.commit()
                
                winning_team = cur.fetchone()
                socketio.emit('player_sold', {
                    'auction_id': auction_id,
                    'player_name': player_name,
                    'team_name': winning_team['name'],
                    'price': sold_price,
                    'winning_team_id': winning_team_id,
                    'new_budget': winning_team['budget']
                })
                log_activity(f"Player '{player_name}' was sold to '{winning_team['name']}' for ₹{sold_price:.2f}.")
                broadcast_stats()

            # सक्रिय बिड से ऑक्शन को हटा दें
            if auction_id in active_bids:
                active_bids.pop(auction_id, None)

        except Exception as e:
            print(f"Error in end_bidding: {e}")
            conn.rollback()
        finally:
            cur.close()

@socketio.on('connect')
def handle_connect(auth=None):
    if 'username' in session:
        conn = get_db_connection()
        cur = get_dict_cursor(conn)
        try:
            if DATABASE_URL:
                cur.execute("SELECT message, timestamp FROM activity_log ORDER BY id DESC LIMIT 50")
            else:
                cur.execute("SELECT message, timestamp FROM activity_log ORDER BY id DESC LIMIT 50")
            history = cur.fetchall()
            # Reverse order so oldest are first
            history_data = [{'message': row['message'], 'timestamp': row['timestamp']} for row in reversed(history)]
            emit('activity_history', history_data)
        except Exception as e:
            print(f"Error fetching activity history: {e}")
        finally:
            cur.close()
        print(f"User {session['username']} connected.")

@socketio.on('get_all_timers')
def handle_get_all_timers():
    """क्लाइंट को सभी सक्रिय ऑक्शन टाइमर भेजता है।"""
    timers_data = {}
    current_time = time.time()
    for auction_id, data in active_bids.items():
        time_left = max(0, int(data['end_time'] - current_time))
        timers_data[auction_id] = time_left
    emit('all_timers', timers_data)


@socketio.on('place_bid')
def handle_place_bid(data):
    if not is_approved_bidder():
        emit('bid_status', {'success': False, 'message': 'You must be a verified team manager to bid.'})
        return
        
    auction_id = data.get('auction_id')
    new_bid = float(data.get('bid_amount'))
    username = session.get('username')
    
    if not auction_id or new_bid is None or new_bid <= 0:
        emit('bid_status', {'success': False, 'message': 'Invalid bid amount or auction ID.'})
        return

    conn = get_db_connection()
    cur = get_dict_cursor(conn)

    try:
        # Fetch team info and budget
        if DATABASE_URL:
            cur.execute("SELECT u.*, t.budget FROM users u JOIN teams t ON u.team_id = t.id WHERE u.username = %s", (username,))
        else:
            cur.execute("SELECT u.*, t.budget FROM users u JOIN teams t ON u.team_id = t.id WHERE u.username = ?", (username,))
        user = cur.fetchone()
        
        if not user or not user['team_id']:
            cur.close()
            emit('bid_status', {'success': False, 'message': 'You are not assigned to a team.', 'auction_id': auction_id})
            return
        
        if not user['can_bid']:
            cur.close()
            emit('bid_status', {'success': False, 'message': 'Your account is not authorized to place bids.', 'auction_id': auction_id})
            return
            
        team_budget = user['budget']
        
        if new_bid > team_budget:
            cur.close()
            emit('bid_status', {'success': False, 'message': f'Bid exceeds your team budget of ₹{team_budget:.2f}.', 'auction_id': auction_id})
            return
            
        # Fetch auction info
        if DATABASE_URL:
            cur.execute("SELECT * FROM auctions WHERE id = %s", (auction_id,))
        else:
            cur.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,))
        auction = cur.fetchone()
        
        if auction and auction['status'] == 'live':
            current_price = auction['current_price']
            
            if new_bid > current_price:
                # Update auction in DB
                if DATABASE_URL:
                    cur.execute("UPDATE auctions SET current_price = %s, highest_bidding_team_id = %s WHERE id = %s", (new_bid, user['team_id'], auction_id))
                    cur.execute("SELECT name FROM teams WHERE id = %s", (user['team_id'],))
                else:
                    cur.execute("UPDATE auctions SET current_price = ?, highest_bidding_team_id = ? WHERE id = ?", (new_bid, user['team_id'], auction_id))
                    cur.execute("SELECT name FROM teams WHERE id = ?", (user['team_id'],))
                    
                team = cur.fetchone()
                conn.commit()
                cur.close()

                # Cancel previous timer and start a new one
                if auction_id in active_bids:
                    active_bids[auction_id]['thread'].cancel()
                
                timer_thread = Timer(BID_DURATION, end_bidding, args=[auction_id])
                timer_thread.start()
                
                end_time = time.time() + BID_DURATION
                active_bids[auction_id] = {'thread': timer_thread, 'end_time': end_time}

                socketio.emit('auction_update', {
                    'auction_id': auction_id,
                    'new_price': new_bid,
                    'bidder': team['name'],
                    'time_left': BID_DURATION
                })

                emit('bid_status', {'success': True, 'message': f'Bid of {new_bid} placed for {team["name"]}!', 'auction_id': auction_id})
                
                log_activity(f"Team '{team['name']}' bid ₹{new_bid:.2f} on '{auction['title']}'.")

            else:
                cur.close()
                emit('bid_status', {'success': False, 'message': f'Bid must be strictly higher than the current price: ₹{current_price:.2f}', 'auction_id': auction_id})
        else:
            cur.close()
            emit('bid_status', {'success': False, 'message': 'Auction is not live or does not exist.', 'auction_id': auction_id})
    
    except Exception as e:
        print(f"Error in handle_place_bid: {e}")
        conn.rollback()
        cur.close()
        emit('bid_status', {'success': False, 'message': f'An internal error occurred: {e}', 'auction_id': auction_id})
        

if __name__ == '__main__':
    # Use 'PORT' environment variable for Render, default to 5000 for local dev
    port = int(os.environ.get('PORT', 5000))
    # Use gunicorn only in production (Render)
    if 'PORT' in os.environ:
        # Note: gunicorn is used via Procfile for deployment
        # This block is usually for local testing only
        print(f"Running in production mode. Gunicorn/Render will handle starting the app on port {port}.")
    else:
        # Run in development mode (local)
        print(f"Running in development mode on http://127.0.0.1:{port}")
        socketio.run(app, debug=True, port=port)
