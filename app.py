from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_file
from flask_login import LoginManager
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import sqlite3
import firebase_admin
from firebase_admin import credentials, auth
from functools import wraps
from flask import send_file
from datetime import datetime
import io
import requests
import random
import base64
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import inch
from dateutil.relativedelta import relativedelta
import google.generativeai as genai


cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred)

GOOGLE_AI_API_KEY = "AIzaSyCH5B-jMBUUoJN4g7NxjXJi-nOZg7xx3Xw"
BREVO_API_KEY = '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'
FROM_EMAIL = 'openslot61@gmail.com'
DATABASE_FILE = 'invoices.db'

genai.configure(api_key=GOOGLE_AI_API_KEY)

app = Flask(__name__)
app.secret_key = 'your_secret_key'

def send_verification_email(to_email, code, user_name='User'):
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {"api-key": BREVO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "sender": {"name": "Invoice Manager", "email": FROM_EMAIL},
        "to": [{"email": to_email, "name": user_name}],
        "subject": "Verify Your Email",
        "htmlContent": f"<h2>Your verification code is: {code}</h2>"
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"Verification email sent to {to_email}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send email: {e}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def init_db():
    """Consolidated function to initialize the database and create all tables."""
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
        
        # users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                firebase_uid TEXT UNIQUE NOT NULL,
                verified INTEGER DEFAULT 0
            )
        ''')

        # clients table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                user_id TEXT NOT NULL,
                hourly_rate REAL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (firebase_uid)
            )
        ''')

        # invoices table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('Unpaid', 'Paid', 'Partially Paid')),
                invoice_date TEXT,
                user_id TEXT NOT NULL,
                due_date TEXT,
                client_id INTEGER,
                hours_worked REAL DEFAULT 0,
                FOREIGN KEY (client_id) REFERENCES clients (id)
            )
        ''')

        # payments table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                amount_paid REAL NOT NULL,
                payment_date TEXT NOT NULL,
                FOREIGN KEY (invoice_id) REFERENCES invoices (id)
            )
        ''')
        
        # calendar_events table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_date TEXT NOT NULL,
                user_id TEXT NOT NULL
            )
        ''')

        conn.commit()
    conn.close()

from firebase_admin import exceptions as firebase_exceptions

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            # create user in firebase
            user = auth.create_user(email=email, password=password)
            print(f"Firebase user created with UID: {user.uid}")

        # catch specific firebase error
        except auth.EmailAlreadyExistsError:
            print(f"Registration failed: Email '{email}' already exists in Firebase.")
            return render_template('register.html', error="This email is already registered. Please try to log in.")
        
        except firebase_exceptions.FirebaseError as fe:
            print(f"Firebase error: {fe}")
            return render_template('register.html', error="There was a problem creating your Firebase account.")

        try:
            # insert user into sqlite database
            with sqlite3.connect('invoices.db', timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO users (email, firebase_uid) VALUES (?, ?)",
                    (email, user.uid)
                )
                conn.commit()
                print("User added to local database.")

        except sqlite3.IntegrityError:
            print("This email already exists in the database.")
            return render_template('register.html', error="This email is already registered.")
        except sqlite3.OperationalError as oe:
            if "locked" in str(oe).lower():
                print("SQLite is locked.")
                return render_template('register.html', error="System is busy. Try again shortly.")
            raise

        # generate verification code and send email
        verification_code = str(random.randint(100000, 999999))
        session['pending_email'] = email
        session['verify_code'] = verification_code

        try:
            send_verification_email(email, verification_code, user_name=email.split('@')[0])
            print("Verification email sent.")
        except Exception as e:
            print("Error sending email:", e)

        return redirect(url_for('verify_email'))

    return render_template('register.html')

@app.route('/')
@login_required
def index():
    user_id = session.get('user')

    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM invoices WHERE user_id = ?", (user_id,))
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM invoices WHERE user_id = ? AND status = 'Paid'", (user_id,))
    paid = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM invoices WHERE user_id = ? AND status = 'Unpaid'", (user_id,))
    unpaid = cursor.fetchone()[0]

    conn.close()

    return render_template('index.html', total=total, paid=paid, unpaid=unpaid)

# view invoices page
@app.route('/invoices')
@login_required
def view_invoices():
    user_id = session.get('user')
    
    status_filter = request.args.get('status', None)

    query = """
        SELECT
            i.id, i.customer_name, i.amount, i.status, i.due_date,
            IFNULL(p.total_paid, 0) as paid_amount
        FROM invoices i
        LEFT JOIN
            (SELECT invoice_id, SUM(amount_paid) as total_paid FROM payments GROUP BY invoice_id) p
        ON i.id = p.invoice_id
        WHERE i.user_id = ?
    """
    params = [user_id]

    if status_filter:
        query += " AND i.status = ?"
        params.append(status_filter)

    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        invoices = conn.execute(query, tuple(params)).fetchall()
    return render_template('view_invoices.html', invoices=invoices, filter_status=status_filter)

# edit an invoice
@app.route('/edit_invoice/<int:invoice_id>', methods=['GET', 'POST'])
@login_required
def edit_invoice(invoice_id):
    user_id = session['user']

    if request.method == 'POST':
        try:
            new_hours = float(request.form['hours_worked'])
            new_due_date = request.form['due_date']
        except (ValueError, TypeError):
            return "Invalid input format.", 400
        
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            client_data = cursor.execute("""
                SELECT c.hourly_rate 
                FROM invoices i 
                JOIN clients c ON i.client_id = c.id 
                WHERE i.id = ? AND i.user_id = ?
            """, (invoice_id, user_id)).fetchone()

            if not client_data:
                return "Invoice not found or you do not have permission.", 404

            new_amount = new_hours * (client_data['hourly_rate'] or 0)

            cursor.execute("""
                UPDATE invoices 
                SET hours_worked = ?, due_date = ?, amount = ?
                WHERE id = ? AND user_id = ?
            """, (new_hours, new_due_date, new_amount, invoice_id, user_id))
            conn.commit()

        return redirect(url_for('view_invoices'))

    else:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.row_factory = sqlite3.Row
            invoice = conn.execute("SELECT * FROM invoices WHERE id = ? AND user_id = ?", (invoice_id, user_id)).fetchone()
        
        if not invoice:
            return "Invoice not found.", 404
            
        return render_template('edit_invoice.html', invoice=invoice)
    
# delete an invoice
@app.route('/delete/<int:invoice_id>')
@login_required
def delete_invoice(invoice_id):
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
    cursor.execute('DELETE FROM invoices WHERE id = ?', (invoice_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('view_invoices'))

@app.route('/add_event', methods=['POST'])
@login_required
def add_event():
    user_id = session.get('user')
    data = request.get_json()
    title = data.get('title')
    date = data.get('date')
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
    cursor.execute("INSERT INTO calendar_events (title, start_date) VALUES (?, ?)", (title, date))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/update_event', methods=['POST'])
@login_required
def update_event():
    data = request.get_json()
    event_id = data.get('id')
    new_date = data.get('date')
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
    cursor.execute("UPDATE calendar_events SET date = ? WHERE id = ?", (new_date, event_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/delete_event/<int:event_id>', methods=['DELETE'])
@login_required
def delete_event(event_id):
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
    cursor.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/get_events')
@login_required
def get_events():
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
    events = []
    # add invoice due dates to calendar
    cursor.execute("SELECT id, customer_name, due_date FROM invoices WHERE due_date IS NOT NULL AND status != 'Paid'")
    for row in cursor.fetchall():
        events.append({
            'id': f'due-{row[0]}',
            'title': f'Due: {row[1]}',
            'start': row[2],
            'color': 'red'
        })

    # custom events
    cursor.execute("SELECT id, title, start_date FROM calendar_events")
    for row in cursor.fetchall():
        events.append({
            'id': row[0],
            'title': row[1],
            'start': row[2],
            'color': 'green'
        })

    conn.close()
    return jsonify(events)

@app.route('/login', methods=['GET'])
def login():
    return render_template('login.html')        

@app.route('/sessionLogin', methods=['POST'])
def session_login():
    id_token = request.json.get('idToken')

    try:
        decoded_token = auth.verify_id_token(id_token)
        user_email = decoded_token.get('email')
        user_uid = decoded_token['uid']

        with sqlite3.connect(DATABASE_FILE) as conn:
            cursor = conn.cursor()
        cursor.execute("SELECT verified FROM users WHERE firebase_uid = ?", (user_uid,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'success': False, 'message': 'User not found in database'}), 403

        if result[0] != 1:
            # user is unverified and send code again
            verification_code = str(random.randint(100000, 999999))

            session['verify_code'] = verification_code
            session['pending_email'] = user_email

            # send email again
            send_verification_email(user_email, verification_code, user_name=user_email.split('@')[0])

            return jsonify({'success': False, 'message': 'Email verification required. Verification code has been re-sent.'}), 403

        # login successful
        session['user'] = user_uid
        session['username'] = user_email
        session['verified'] = True
        return jsonify({'success': True})

    except Exception as e:
        print("Login error:", e)
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
@app.route('/verify', methods=['GET', 'POST'])
def verify_email():
    if request.method == 'POST':
        input_code = request.form['code']
        if input_code == session.get('verify_code'):
            session['verified'] = True
            session.pop('verify_code', None)

            # update user as verified in database
            with sqlite3.connect(DATABASE_FILE) as conn:
                cursor = conn.cursor()
            cursor.execute("UPDATE users SET verified = 1 WHERE email = ?", (session.get('pending_email'),))
            conn.commit()
            conn.close()

            return redirect(url_for('login'))
        else:
            return render_template('verify.html', error="Invalid code.")
    return render_template('verify.html')

@app.route('/generate_receipt')
@login_required
def generate_receipt():
    user_id = session.get('user')

    # get unpaid invoices for this user
    with sqlite3.connect(DATABASE_FILE) as conn:
        cursor = conn.cursor()
    cursor.execute("""
    SELECT id, customer_name, amount, due_date
    FROM invoices
    WHERE status = 'Unpaid' AND user_id = ?
""", (user_id,))
    unpaid_invoices = cursor.fetchall()
    conn.close()

    # create pdf in memory
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "Unpaid Invoices Summary")

    c.setFont("Helvetica", 10)
    c.drawString(50, height - 65, f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, height - 100, "ID")
    c.drawString(100, height - 100, "Customer Name")
    c.drawString(280, height - 100, "Amount")
    c.drawString(400, height - 100, "Due Date")

    c.setFont("Helvetica", 12)
    y = height - 120
    total = 0

    for invoice in unpaid_invoices:
        invoice_id, name, amount, due_date, *_ = invoice
        
        c.drawString(50, y, str(invoice_id))
        c.drawString(100, y, name)
        c.drawString(280, y, f"${amount:.2f}")
        c.drawString(400, y, due_date or "N/A")
        
        total += amount
        y -= 20
        
        if y < 50:
            c.showPage()
            c.setFont("Helvetica-Bold", 12)
            c.drawString(50, height - 100, "ID")
            c.drawString(100, height - 100, "Customer Name")
            c.drawString(280, height - 100, "Amount")
            c.drawString(400, height - 100, "Due Date")
            c.setFont("Helvetica", 12)
            y = height - 120

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y - 20, f"Total Unpaid Amount: ${total:.2f}")

    c.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="unpaid_invoices.pdf",
        mimetype='application/pdf'
    )

@app.route('/record_payment/<int:invoice_id>', methods=['POST'])
@login_required
def record_payment(invoice_id):
    payment_amount = float(request.form['payment_amount'])
    payment_date = datetime.now().strftime('%Y-%m-%d')

    with sqlite3.connect('invoices.db') as conn:
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO payments (invoice_id, amount_paid, payment_date) VALUES (?, ?, ?)",
            (invoice_id, payment_amount, payment_date)
        )

        cursor.execute(
            "SELECT SUM(amount_paid) FROM payments WHERE invoice_id = ?",
            (invoice_id,)
        )
        total_paid = cursor.fetchone()[0] or 0

        cursor.execute("SELECT amount FROM invoices WHERE id = ?", (invoice_id,))
        invoice_amount = cursor.fetchone()[0]

        new_status = 'Partially Paid'
        if total_paid >= invoice_amount:
            new_status = 'Paid'
        
        cursor.execute(
            "UPDATE invoices SET status = ? WHERE id = ?",
            (new_status, invoice_id)
        )
        conn.commit()

    return redirect(url_for('view_invoices'))

@app.route('/add_client', methods=['POST'])
@login_required
def add_client():
    user_id = session['user']
    name = request.form['name']
    email = request.form['email']
    
    try:
        rate_input = request.form.get('hourly_rate') or '0'
        hourly_rate = float(rate_input)
    except ValueError:
        hourly_rate = 0

    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.execute(
            "INSERT INTO clients (name, email, user_id, hourly_rate) VALUES (?, ?, ?, ?)",
            (name, email, user_id, hourly_rate)
        )
        conn.commit()
        
    return redirect(url_for('manage_clients'))

@app.route('/clients')
@login_required
def manage_clients():
    user_id = session['user']
    with sqlite3.connect('invoices.db') as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM clients WHERE user_id = ?", (user_id,))
        clients = cursor.fetchall()
    return render_template('clients.html', clients=clients)

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_invoice():
    user_id = session['user']
    
    if request.method == 'POST':
        invoice_date = datetime.now().strftime('%Y-%m-%d')
        
        try:
            hours_worked = float(request.form['hours_worked'])
            client_id = int(request.form['client_id'])
            due_date = request.form['due_date']
            
            with sqlite3.connect(DATABASE_FILE) as conn:
                cursor = conn.cursor()
                
                client_info = cursor.execute(
                    "SELECT name, hourly_rate FROM clients WHERE id = ? AND user_id = ?",
                    (client_id, user_id)
                ).fetchone()

                if not client_info:
                    return "Client not found or you do not have permission.", 404
                
                client_name, hourly_rate = client_info
                
                total_amount = hours_worked * (hourly_rate or 0)

                cursor.execute("""
                    INSERT INTO invoices (customer_name, amount, status, invoice_date, user_id, due_date, client_id, hours_worked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (client_name, total_amount, 'Unpaid', invoice_date, user_id, due_date, client_id, hours_worked))
                conn.commit()
            
            return redirect(url_for('view_invoices'))

        except ValueError:
            return "Invalid input. Please ensure hours worked is a valid number.", 400
        except Exception as e:
            print(f"An unexpected error occurred in add_invoice: {e}")
            return "An unexpected error occurred. Please try again.", 500

    else:
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.row_factory = sqlite3.Row
            clients = conn.execute("SELECT id, name FROM clients WHERE user_id = ?", (user_id,)).fetchall()
        return render_template('add_invoice.html', clients=clients)

@app.route('/download_unpaid_invoices')
@login_required
def download_unpaid_invoices_pdf():
    user_id = session['user']

    # get all unpaid or partially paid invoices for the current user
    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT
                i.id, i.customer_name, i.amount, i.due_date,
                IFNULL(p.total_paid, 0) as paid_amount
            FROM invoices i
            LEFT JOIN (
                SELECT invoice_id, SUM(amount_paid) as total_paid FROM payments GROUP BY invoice_id
            ) p ON i.id = p.invoice_id
            WHERE i.user_id = ? AND i.status IN ('Unpaid', 'Partially Paid')
            ORDER BY i.due_date ASC
        """
        invoices = conn.execute(query, (user_id,)).fetchall()

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=landscape(A4))
    width, height = landscape(A4)
    
    c.setFont("Helvetica-Bold", 18)
    c.drawString(inch, height - inch, "Summary of Unpaid Invoices")
    
    c.setFont("Helvetica", 10)
    c.drawString(inch, height - inch - 20, f"Report generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    c.setFont("Helvetica-Bold", 10)
    y_position = height - (inch * 2)
    headers = ["ID", "Customer Name", "Due Date", "Total Amount", "Amount Paid", "Balance Due"]
    x_positions = [inch, inch * 2, inch * 4.5, inch * 6.5, inch * 8, inch * 9.5]
    
    for i, header in enumerate(headers):
        c.drawString(x_positions[i], y_position, header)
    
    c.line(inch, y_position - 5, width - inch, y_position - 5)

    c.setFont("Helvetica", 9)
    y_position -= 20
    total_balance_due = 0

    for invoice in invoices:
        balance_due = invoice['amount'] - invoice['paid_amount']
        total_balance_due += balance_due

        row_data = [
            str(invoice['id']),
            invoice['customer_name'],
            invoice['due_date'] or 'N/A',
            f"${invoice['amount']:.2f}",
            f"${invoice['paid_amount']:.2f}",
            f"${balance_due:.2f}"
        ]
        
        for i, data in enumerate(row_data):
            c.drawString(x_positions[i], y_position, data)

        y_position -= 20
        if y_position < inch:
            c.showPage()
            c.setFont("Helvetica-Bold", 10)
            y_position = height - inch

    c.line(inch, y_position + 10, width - inch, y_position + 10)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(inch * 8, y_position - 5, "Total Balance Due:")
    c.drawString(inch * 9.5, y_position - 5, f"${total_balance_due:.2f}")

    c.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"unpaid_invoices_{datetime.now().strftime('%Y-%m-%d')}.pdf",
        mimetype='application/pdf'
    )

def generate_full_tax_report_pdf(invoices):
    """
    Generates a detailed PDF report of ALL invoices for tax/archival purposes.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=landscape(A4))
    width, height = landscape(A4)

    c.setFont("Helvetica-Bold", 18)
    c.drawString(inch, height - inch, "Full Invoice Report")
    c.setFont("Helvetica", 10)
    c.drawString(inch, height - inch - 20, f"Report generated on: {datetime.now().strftime('%Y-%m-%d')}")

    c.setFont("Helvetica-Bold", 9)
    y_position = height - (inch * 1.8)
    headers = ["ID", "Customer", "Invoice Date", "Due Date", "Status", "Total Amount", "Amount Paid", "Balance"]
    x_positions = [inch, inch * 2, inch * 4.2, inch * 5.5, inch * 6.8, inch * 8, inch * 9.3, inch * 10.5]
    
    for i, header in enumerate(headers):
        c.drawString(x_positions[i], y_position, header)
    c.line(inch, y_position - 5, width - inch, y_position - 5)

    c.setFont("Helvetica", 8)
    y_position -= 20
    total_invoiced = 0
    total_paid = 0

    for invoice in invoices:
        balance = invoice['amount'] - invoice['paid_amount']
        total_invoiced += invoice['amount']
        total_paid += invoice['paid_amount']

        row_data = [
            str(invoice['id']),
            invoice['customer_name'],
            invoice['invoice_date'] or 'N/A',
            invoice['due_date'] or 'N/A',
            invoice['status'],
            f"${invoice['amount']:.2f}",
            f"${invoice['paid_amount']:.2f}",
            f"${balance:.2f}"
        ]
        
        for i, data in enumerate(row_data):
            c.drawString(x_positions[i], y_position, data)

        y_position -= 20
        if y_position < inch:
            c.showPage()
            y_position = height - inch

    c.line(inch, y_position + 10, width - inch, y_position + 10)
    c.setFont("Helvetica-Bold", 12)
    y_position -= 10
    c.drawRightString(width - inch - (inch * 3), y_position, "Total Amount Invoiced:")
    c.drawRightString(width - inch - (inch * 1.5), y_position, f"${total_invoiced:.2f}")
    y_position -= 20
    c.drawRightString(width - inch - (inch * 3), y_position, "Total Amount Paid:")
    c.drawRightString(width - inch - (inch * 1.5), y_position, f"${total_paid:.2f}")

    c.save()
    buffer.seek(0)
    return buffer

def calculate_linear_regression(x_values, y_values):
    n = len(x_values)
    if n == 0: return 0, 0
    sum_x, sum_y = sum(x_values), sum(y_values)
    sum_xy = sum(x * y for x, y in zip(x_values, y_values))
    sum_xx = sum(x * x for x in x_values)
    m = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x * sum_x) if (n * sum_xx - sum_x * sum_x) != 0 else 0
    b = (sum_y - m * sum_x) / n
    return m, b

@app.route('/download_full_report')
@login_required
def download_full_report_pdf():
    user_id = session['user']

    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT
                i.id, i.customer_name, i.amount, i.due_date, i.status, i.invoice_date,
                IFNULL(p.total_paid, 0) as paid_amount
            FROM invoices i
            LEFT JOIN (
                SELECT invoice_id, SUM(amount_paid) as total_paid FROM payments GROUP BY invoice_id
            ) p ON i.id = p.invoice_id
            WHERE i.user_id = ?
            ORDER BY i.id ASC
        """
        all_invoices = conn.execute(query, (user_id,)).fetchall()

    pdf_buffer = generate_full_tax_report_pdf(all_invoices)

    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"full_invoice_report_{datetime.now().strftime('%Y-%m-%d')}.pdf",
        mimetype='application/pdf'
    )

def generate_invoice_pdf(invoice_data):
    """
    Generates a detailed, professional-looking PDF for a single invoice.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(inch, height - inch, "OpenSlot Inc.")
    c.setFont("Helvetica", 10)
    c.drawString(inch, height - inch - 15, "110 Gilba Rd, Girraween NSW 2145")
    c.setFont("Helvetica-Bold", 24)
    c.drawRightString(width - inch, height - inch, "INVOICE")

    details_y = height - inch - 60
    c.setFont("Helvetica", 10)
    c.drawString(inch, details_y, f"Invoice #: {invoice_data['id']}")
    c.drawString(inch, details_y - 15, f"Date Issued: {invoice_data['invoice_date']}")
    c.drawString(inch, details_y - 30, f"Due Date: {invoice_data['due_date']}")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(width - (inch * 4), details_y, "BILL TO:")
    c.setFont("Helvetica", 10)
    c.drawString(width - (inch * 4), details_y - 15, invoice_data['customer_name'])
    c.drawString(width - (inch * 4), details_y - 30, invoice_data['client_email'] or '')

    table_y = details_y - 80
    c.setFont("Helvetica-Bold", 10)
    c.line(inch, table_y + 15, width - inch, table_y + 15)
    c.drawString(inch, table_y, "DESCRIPTION")
    c.drawString(inch * 4.5, table_y, "HOURS")
    c.drawString(inch * 5.5, table_y, "RATE")
    c.drawRightString(width - inch, table_y, "AMOUNT")
    c.line(inch, table_y - 5, width - inch, table_y - 5)
    
    table_y -= 20
    c.setFont("Helvetica", 10)
    hourly_rate = invoice_data['amount'] / invoice_data['hours_worked'] if invoice_data['hours_worked'] else 0
    c.drawString(inch + 5, table_y, f"Invoice for {invoice_data['customer_name']}")
    c.drawString(inch * 4.5, table_y, f"{invoice_data['hours_worked']:.2f}")
    c.drawString(inch * 5.5, table_y, f"${hourly_rate:.2f}")
    c.drawRightString(width - inch, table_y, f"${invoice_data['amount']:.2f}")

    balance_due = invoice_data['amount'] - invoice_data['paid_amount']
    
    totals_y = table_y - 50

    c.setFont("Helvetica", 10)
    c.drawRightString(width - inch - inch, totals_y, "Subtotal:")
    c.drawRightString(width - inch, totals_y, f"${invoice_data['amount']:.2f}")
    
    if invoice_data['paid_amount'] > 0:
        totals_y -= 20
        c.drawRightString(width - inch - inch, totals_y, "Amount Paid:")
        c.drawRightString(width - inch, totals_y, f"-${invoice_data['paid_amount']:.2f}")

    totals_y -= 5
    c.line(width - inch - (inch * 2), totals_y, width - inch, totals_y)
    
    totals_y -= 20
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(width - inch - inch, totals_y, "Balance Due:")
    c.drawRightString(width - inch, totals_y, f"${balance_due:.2f}")

    status = invoice_data['status']
    if status != 'Unpaid':
        c.saveState()
        c.setFont("Helvetica-Bold", 48)
        c.setStrokeGray(0.8)
        if status == 'Paid':
            c.setFillColorRGB(0, 0.6, 0, 0.3)
            stamp_text = "PAID"
        else:
            c.setFillColorRGB(1, 0.6, 0, 0.3)
            stamp_text = "PARTIALLY PAID"
        c.translate(width / 2.0, height / 2.0)
        c.rotate(45)
        c.drawCentredString(0, 0, stamp_text)
        c.restoreState()

    c.setFont("Helvetica-Oblique", 9)
    c.drawCentredString(width / 2.0, inch / 2.0, "Thank you for your business!")

    c.save()
    buffer.seek(0)
    return buffer

@app.route('/download_invoice/<int:invoice_id>')
@login_required
def download_single_invoice_pdf(invoice_id):
    user_id = session['user']

    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT
                i.*,
                c.email as client_email,
                IFNULL(p.total_paid, 0) as paid_amount
            FROM invoices i
            JOIN clients c ON i.client_id = c.id
            LEFT JOIN (
                SELECT invoice_id, SUM(amount_paid) as total_paid
                FROM payments
                GROUP BY invoice_id
            ) p ON i.id = p.invoice_id
            WHERE i.id = ? AND i.user_id = ?
        """
        invoice = conn.execute(query, (invoice_id, user_id)).fetchone()

    if not invoice:
        return "Invoice not found or you do not have permission.", 404

    pdf_buffer = generate_invoice_pdf(invoice)

    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"invoice-{invoice['id']}-{invoice['customer_name']}.pdf",
        mimetype='application/pdf'
    )

@app.route('/delete_client/<int:client_id>')
@login_required
def delete_client(client_id):
    user_id = session['user']
    
    with sqlite3.connect('invoices.db') as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM clients WHERE id = ? AND user_id = ?", 
            (client_id, user_id)
        )
        conn.commit()
        
    return redirect(url_for('manage_clients'))

@app.route('/edit_client/<int:client_id>', methods=['GET', 'POST'])
@login_required
def edit_client(client_id):
    user_id = session['user']
    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()

        if request.method == 'POST':
            new_name = request.form['name']
            new_email = request.form['email']
            
            try:
                rate_input = request.form.get('hourly_rate') or '0'
                new_rate = float(rate_input)
            except ValueError:
                new_rate = 0
            
            cursor.execute(
                "UPDATE clients SET name = ?, email = ?, hourly_rate = ? WHERE id = ? AND user_id = ?",
                (new_name, new_email, new_rate, client_id, user_id)
            )
            conn.commit()
            return redirect(url_for('manage_clients'))

        else: 
            cursor.execute("SELECT * FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id))
            client = cursor.fetchone()

            if client is None:
                return "Client not found or you don't have permission to edit.", 404
            
            return render_template('edit_client.html', client=client)

@app.route('/api/cash_flow_data')
@login_required
def cash_flow_data():
    """
    API endpoint to provide cash flow data grouped by month.
    """
    user_id = session['user']
    
    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        
        query = """
            SELECT
                strftime('%Y-%m', p.payment_date) as month,
                SUM(p.amount_paid) as monthly_total
            FROM
                payments p
            JOIN
                invoices i ON p.invoice_id = i.id
            WHERE
                i.user_id = ?
            GROUP BY
                month
            ORDER BY
                month ASC;
        """
        
        results = conn.execute(query, (user_id,)).fetchall()

        labels = [row['month'] for row in results]
        data = [row['monthly_total'] for row in results]

        return jsonify({'labels': labels, 'data': data})

@app.route('/analytics')
@login_required
def analytics_page():
    return render_template('analytics.html')

@app.route('/api/predicted_growth_data')
@login_required
def predicted_growth_data():
    user_id = session['user']
    
    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT strftime('%Y-%m', p.payment_date) as month, SUM(p.amount_paid) as monthly_total
            FROM payments p JOIN invoices i ON p.invoice_id = i.id
            WHERE i.user_id = ? GROUP BY month ORDER BY month ASC;
        """
        results = conn.execute(query, (user_id,)).fetchall()

    if not results:
        return jsonify({'labels': [], 'actual_data': [], 'predicted_data': []})

    actual_labels = [row['month'] for row in results]
    actual_data = [row['monthly_total'] for row in results]

    if len(results) == 1:
        m = 0
        b = actual_data[0]
    else: 
        numeric_x = list(range(len(actual_data)))
        m, b = calculate_linear_regression(numeric_x, actual_data)
    last_month = datetime.strptime(actual_labels[-1], '%Y-%m').date()
    future_labels = [(last_month + relativedelta(months=i)).strftime('%Y-%m') for i in range(1, 7)]
    
    all_labels = actual_labels + future_labels
    all_numeric_x = list(range(len(all_labels)))

    predicted_data = [m * x + b for x in all_numeric_x]

    return jsonify({
        'labels': all_labels, 
        'actual_data': actual_data, 
        'predicted_data': predicted_data
    })

@app.route('/draft_reminder_email/<int:invoice_id>')
@login_required
def draft_reminder_email(invoice_id):
    user_id = session['user']
    
    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT i.id, i.customer_name, i.amount, i.due_date,
                   IFNULL(p.total_paid, 0) as paid_amount
            FROM invoices i
            LEFT JOIN (
                SELECT invoice_id, SUM(amount_paid) as total_paid FROM payments GROUP BY invoice_id
            ) p ON i.id = p.invoice_id
            WHERE i.id = ? AND i.user_id = ?
        """
        invoice = conn.execute(query, (invoice_id, user_id)).fetchone()

    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    balance_due = invoice['amount'] - invoice['paid_amount']
    due_date = datetime.strptime(invoice['due_date'], '%Y-%m-%d').date()
    days_overdue = (datetime.now().date() - due_date).days

    prompt = ""

    if days_overdue > 0:
        prompt = f"""
        You are a helpful accounting assistant. Your task is to draft a polite reminder for a PAST DUE invoice payment.
        Be friendly but clear that the payment is now overdue.

        Use the following details:
        - Client Name: {invoice['customer_name']}
        - Invoice Number: #{invoice['id']}
        - Balance Due: ${balance_due:.2f}
        - Due Date: {invoice['due_date']}
        - Days Overdue: {days_overdue}

        Draft only the body of the email. Do not mention that an invoice is attached.
        """
    else:
        days_until_due = -days_overdue
        prompt = f"""
        You are a helpful accounting assistant. Your task is to draft a friendly reminder for an UPCOMING invoice payment.
        The tone should be gentle and courteous, not demanding. Please attach the invoice to the email before sending

        Use the following details:
        - Client Name: {invoice['customer_name']}
        - Invoice Number: #{invoice['id']}
        - Balance Due: ${balance_due:.2f}
        - Due Date: {invoice['due_date']}
        - Days Until Due: {days_until_due}

        Draft only the body of the email. Do not mention that an invoice is attached.
        """
    
    try:
        model = genai.GenerativeModel('gemini-1.5-pro-latest')
        response = model.generate_content(prompt)

        return jsonify({'draft': response.text})
        
    except Exception as e:
        print(f"AI generation failed: {e}")
        return jsonify({'error': 'Failed to generate AI draft.'}), 500

@app.route('/api/send_reminder', methods=['POST'])
@login_required
def send_reminder_email():
    user_id = session['user']
    data = request.get_json()
    invoice_id = data.get('invoice_id')
    email_body = data.get('email_body')

    if not invoice_id or not email_body:
        return jsonify({'error': 'Missing data'}), 400

    with sqlite3.connect(DATABASE_FILE) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT
                i.*,
                c.email as client_email,
                IFNULL(p.total_paid, 0) as paid_amount
            FROM invoices i
            JOIN clients c ON i.client_id = c.id
            LEFT JOIN (
                SELECT invoice_id, SUM(amount_paid) as total_paid
                FROM payments GROUP BY invoice_id
            ) p ON i.id = p.invoice_id
            WHERE i.id = ? AND i.user_id = ?
        """
        invoice = conn.execute(query, (invoice_id, user_id)).fetchone()

    if not invoice:
        return jsonify({'error': 'Invoice or client not found'}), 404

    pdf_buffer = generate_invoice_pdf(invoice)
    encoded_pdf = base64.b64encode(pdf_buffer.getvalue()).decode()

    pdf_buffer = generate_invoice_pdf(invoice)
    
    encoded_pdf = base64.b64encode(pdf_buffer.getvalue()).decode()

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }
    
    payload = {
        "sender": {"name": "OpenSlot Inc.", "email": FROM_EMAIL},
        "to": [{"email": invoice['client_email'], "name": invoice['customer_name']}],
        "subject": f"Payment Reminder for Invoice #{invoice['id']}",
        "htmlContent": email_body.replace('\n', '<br>'),
        "attachment": [{
            "content": encoded_pdf,
            "name": f"Invoice for {invoice['customer_name']}.pdf"
        }]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"Reminder email with attachment sent for invoice {invoice_id}")
        return jsonify({'success': True, 'message': 'Email sent successfully!'})
    except requests.exceptions.RequestException as e:
        print(f"Failed to send email via Brevo: {e}")
        return jsonify({'error': 'Failed to send email'}), 500

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']

        try:
            user = auth.get_user_by_email(email)
            reset_code = str(random.randint(100000, 999999))

            session['reset_email'] = email
            session['reset_code'] = reset_code

            send_verification_email(email, reset_code, user_name=email.split('@')[0])
            return redirect(url_for('verify_reset_code'))
        except Exception as e:
            return render_template('forgot_password.html', error="Email not found.")
    
    return render_template('forgot_password.html')

@app.route('/verify_reset_code', methods=['GET', 'POST'])
def verify_reset_code():
    if request.method == 'POST':
        code = request.form['code']
        if code == session.get('reset_code'):
            return redirect(url_for('reset_password'))
        else:
            return render_template('verify_reset_code.html', error="Invalid verification code.")
    return render_template('verify_reset_code.html')

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'POST':
        new_password = request.form['new_password']
        email = session.get('reset_email')

        try:
            user = auth.get_user_by_email(email)
            auth.update_user(user.uid, password=new_password)

            session.pop('reset_email', None)
            session.pop('reset_code', None)

            return redirect(url_for('login'))
        except Exception as e:
            return render_template('reset_password.html', error="Could not reset password.")
    
    return render_template('reset_password.html')


@app.route('/logout')
@login_required
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))


if __name__ == '__main__':
    init_db()
    app.run(debug=True)