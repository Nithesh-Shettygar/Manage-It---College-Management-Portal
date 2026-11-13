import os
import io
import csv
import re
import hmac
import threading
import webbrowser
from contextlib import contextmanager
from datetime import date, datetime

from flask import (Flask, request, redirect, session, jsonify, render_template,
                   send_from_directory, make_response, abort, Response)
from jinja2 import TemplateNotFound
from werkzeug.utils import secure_filename
import mysql.connector

BACKEND_DIR = os.path.abspath(os.path.dirname(__file__))
BASE_DIR = os.path.abspath(os.path.join(BACKEND_DIR, '..'))

app = Flask(__name__, template_folder=BASE_DIR, static_folder=None)
app.secret_key = 'college_portal_secure_secret_key'

DB_CONFIG = {'host': 'localhost', 'user': 'root', 'password': ''}
DB_NAME = 'college_portal'
PORT = 5001

# Shared secret between this portal and the face attendance system.
# Set the SAME value in the face system (environment variable FACE_API_KEY).
FACE_API_KEY = os.environ.get('FACE_API_KEY', 'change-this-face-key')

DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
DEFAULT_SLOTS = [
    '09:00 AM - 10:00 AM',
    '10:00 AM - 11:00 AM',
    '11:15 AM - 12:15 PM',
    '01:00 PM - 02:00 PM',
    '02:00 PM - 03:00 PM',
    '03:15 PM - 04:15 PM',
]


def get_db():
    return mysql.connector.connect(**DB_CONFIG, database=DB_NAME)


@contextmanager
def db_cursor(commit=False):
    """Dict cursor that always closes. Rolls back automatically if an error is raised."""
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        yield cursor
        if commit:
            conn.commit()
    finally:
        cursor.close()
        conn.close()


def initialize_database():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
        cursor.execute(f"USE {DB_NAME}")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                full_name VARCHAR(100) NOT NULL,
                role_title VARCHAR(50) DEFAULT 'Super Admin'
            )""")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS faculty (
                id INT AUTO_INCREMENT PRIMARY KEY,
                employee_id VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                full_name VARCHAR(100) NOT NULL,
                designation VARCHAR(100) DEFAULT 'Associate Faculty',
                gender VARCHAR(20),
                email VARCHAR(100),
                phone VARCHAR(30),
                dob DATE,
                qualification VARCHAR(255),
                photo VARCHAR(255) DEFAULT ''
            )""")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INT AUTO_INCREMENT PRIMARY KEY,
                registration_no VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                full_name VARCHAR(100) NOT NULL,
                course VARCHAR(50) DEFAULT 'BCA',
                semester INT DEFAULT 1,
                course_sem VARCHAR(50) DEFAULT 'BCA - Sem 1',
                gender VARCHAR(20),
                dob DATE,
                father_name VARCHAR(100),
                mother_name VARCHAR(100),
                phone VARCHAR(30),
                email VARCHAR(100),
                aadhaar VARCHAR(20),
                photo VARCHAR(255) DEFAULT ''
            )""")

        # Courses created by the admin (Course Catalog page)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id INT AUTO_INCREMENT PRIMARY KEY,
                course_code VARCHAR(50) UNIQUE NOT NULL,
                course_name VARCHAR(150) NOT NULL,
                duration VARCHAR(30) NOT NULL DEFAULT '3 Years',
                total_semesters INT NOT NULL DEFAULT 6
            ) ENGINE=InnoDB""")

        # Subjects belong to a course + semester.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INT AUTO_INCREMENT PRIMARY KEY,
                course_code VARCHAR(50) NOT NULL,
                semester INT NOT NULL,
                subject_code VARCHAR(50) NOT NULL,
                subject_name VARCHAR(150) NOT NULL,
                credits INT NOT NULL DEFAULT 4,
                duration VARCHAR(50) NOT NULL DEFAULT '',
                subject_type VARCHAR(20) NOT NULL DEFAULT 'Theory',
                UNIQUE KEY uq_course_subject (course_code, subject_code),
                CONSTRAINT fk_subject_course FOREIGN KEY (course_code)
                    REFERENCES courses(course_code)
                    ON DELETE CASCADE ON UPDATE CASCADE
            ) ENGINE=InnoDB""")

        # Ensure new columns exist if the students table was created earlier
        student_extra_cols = [
            ("course", "VARCHAR(50) DEFAULT 'BCA'"),
            ("semester", "INT DEFAULT 1"),
            ("course_sem", "VARCHAR(50) DEFAULT 'BCA - Sem 1'"),
            ("gender", "VARCHAR(20)"),
            ("dob", "DATE"),
            ("father_name", "VARCHAR(100)"),
            ("mother_name", "VARCHAR(100)"),
            ("phone", "VARCHAR(30)"),
            ("email", "VARCHAR(100)"),
            ("aadhaar", "VARCHAR(20)"),
            ("photo", "VARCHAR(255) DEFAULT ''"),
        ]
        cursor.execute("SHOW COLUMNS FROM students")
        existing_cols = {row[0] for row in cursor.fetchall()}
        for col_name, col_def in student_extra_cols:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE students ADD COLUMN {col_name} {col_def}")

        # Timetable time slots (rows of the grid) per course + semester
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS timetable_slots (
                id INT AUTO_INCREMENT PRIMARY KEY,
                course_code VARCHAR(50) NOT NULL,
                semester INT NOT NULL,
                slot_order INT NOT NULL DEFAULT 0,
                time_label VARCHAR(50) NOT NULL,
                CONSTRAINT fk_slot_course FOREIGN KEY (course_code)
                    REFERENCES courses(course_code)
                    ON DELETE CASCADE ON UPDATE CASCADE
            ) ENGINE=InnoDB""")

        # Timetable cells. type = subject | break | lunch | custom
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS timetable_entries (
                id INT AUTO_INCREMENT PRIMARY KEY,
                slot_id INT NOT NULL,
                day_name VARCHAR(15) NOT NULL,
                entry_type VARCHAR(10) NOT NULL DEFAULT 'subject',
                subject_id INT NULL,
                custom_text VARCHAR(150) NOT NULL DEFAULT '',
                UNIQUE KEY uq_slot_day (slot_id, day_name),
                CONSTRAINT fk_entry_slot FOREIGN KEY (slot_id)
                    REFERENCES timetable_slots(id) ON DELETE CASCADE,
                CONSTRAINT fk_entry_subject FOREIGN KEY (subject_id)
                    REFERENCES subjects(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        # Which faculty teaches which subject (one faculty per subject)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS faculty_assignments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                subject_id INT NOT NULL UNIQUE,
                faculty_id INT NOT NULL,
                CONSTRAINT fk_fa_subject FOREIGN KEY (subject_id)
                    REFERENCES subjects(id) ON DELETE CASCADE,
                CONSTRAINT fk_fa_faculty FOREIGN KEY (faculty_id)
                    REFERENCES faculty(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        # Attendance (one row per student / subject / date)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INT AUTO_INCREMENT PRIMARY KEY,
                student_id INT NOT NULL,
                subject_id INT NOT NULL,
                faculty_id INT NULL,
                att_date DATE NOT NULL,
                status VARCHAR(10) NOT NULL DEFAULT 'Present',
                UNIQUE KEY uq_att (student_id, subject_id, att_date),
                CONSTRAINT fk_att_student FOREIGN KEY (student_id)
                    REFERENCES students(id) ON DELETE CASCADE,
                CONSTRAINT fk_att_subject FOREIGN KEY (subject_id)
                    REFERENCES subjects(id) ON DELETE CASCADE,
                CONSTRAINT fk_att_faculty FOREIGN KEY (faculty_id)
                    REFERENCES faculty(id) ON DELETE SET NULL
            ) ENGINE=InnoDB""")

        # Assessments (internal exam, quiz, assignment ...) created per subject
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assessments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                subject_id INT NOT NULL,
                title VARCHAR(100) NOT NULL,
                category VARCHAR(30) NOT NULL DEFAULT 'Internal Exam',
                max_marks DECIMAL(6,2) NOT NULL DEFAULT 100,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_asm_subject FOREIGN KEY (subject_id)
                    REFERENCES subjects(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        # Marks (one row per student per assessment)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assessment_marks (
                id INT AUTO_INCREMENT PRIMARY KEY,
                assessment_id INT NOT NULL,
                student_id INT NOT NULL,
                marks DECIMAL(6,2) NOT NULL,
                UNIQUE KEY uq_mark (assessment_id, student_id),
                CONSTRAINT fk_mark_asm FOREIGN KEY (assessment_id)
                    REFERENCES assessments(id) ON DELETE CASCADE,
                CONSTRAINT fk_mark_student FOREIGN KEY (student_id)
                    REFERENCES students(id) ON DELETE CASCADE
            ) ENGINE=InnoDB""")

        cursor.execute("SELECT COUNT(*) FROM admins")
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO admins (username, password, full_name, role_title) VALUES (%s,%s,%s,%s)",
                           ('admin', 'admin123', 'Admin User', 'admin'))

        cursor.execute("SELECT COUNT(*) FROM faculty")
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO faculty (employee_id, password, full_name, designation, email) VALUES (%s,%s,%s,%s,%s)",
                           ('FAC104', 'faculty123', 'Prof. Sarah Jenkins', 'Associate Faculty', 'sarah.jenkins@portal.edu'))

        conn.commit()
        cursor.close()
        conn.close()
        print("[INFO] Database initialized successfully!")
    except mysql.connector.Error as err:
        print(f"[ERROR] Database initialization error: {err}")


def no_cache(response):
    resp = make_response(response)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


ROLE_HOME = {
    'student': '/student/view.html',
    'faculty': '/faculty/home.html',
    'admin':   '/admin/dashboard.html',
}


@app.route('/')
@app.route('/index.html')
def index():
    return render_template('index.html')


@app.route('/auth/login.html')
def login_page():
    role = session.get('role')
    if role in ROLE_HOME:
        return redirect(ROLE_HOME[role])
    return no_cache(render_template('auth/login.html'))


@app.route('/<folder>/<path:filename>')
def serve_pages(folder, filename):
    if folder in ROLE_HOME:
        if session.get('role') != folder:
            return redirect('/auth/login.html')
        if not filename.endswith('.html'):
            abort(404)
        try:
            return no_cache(render_template(f'{folder}/{filename}'))
        except TemplateNotFound:
            abort(404)

    if folder == 'components':
        if filename.endswith('.html'):
            try:
                return render_template(f'components/{filename}')
            except TemplateNotFound:
                abort(404)
        return send_from_directory(os.path.join(BASE_DIR, 'components'), filename)

    if folder == 'static':
        return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)

    abort(404)


# ---------------- Auth ----------------
@app.route('/auth/login', methods=['POST'])
def login():
    data = request.form if request.form else (request.get_json(silent=True) or {})
    role = (data.get('role') or '').strip()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    queries = {
        'admin':   ("SELECT * FROM admins WHERE username=%s AND password=%s",
                    lambda u: (u['full_name'], u['username'], u['role_title'])),
        'faculty': ("SELECT * FROM faculty WHERE employee_id=%s AND password=%s",
                    lambda u: (u['full_name'], u['employee_id'], u['designation'])),
        'student': ("SELECT * FROM students WHERE registration_no=%s AND password=%s",
                    lambda u: (u['full_name'], u['registration_no'], u.get('course_sem') or f"{u.get('course', 'BCA')} - Sem {u.get('semester', 1)}")),
    }
    if role not in queries:
        return jsonify({'success': False, 'message': 'Invalid role!'}), 400

    sql, extract = queries[role]
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, (username, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
    except mysql.connector.Error as err:
        print(f"[ERROR] Login DB error: {err}")
        return jsonify({'success': False, 'message': 'Database error.'}), 500

    if not user:
        return jsonify({'success': False, 'message': 'Invalid credentials or role!'}), 401

    name, id_number, title = extract(user)
    session.clear()
    session['user'] = name
    session['role'] = role
    session['id_number'] = id_number
    session['title'] = title
    return jsonify({'success': True, 'role': role})


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/auth/login.html')


@app.route('/api/current-user')
def current_user():
    if 'user' in session:
        return jsonify({
            'logged_in': True,
            'name': session['user'],
            'role': session['role'],
            'id_number': session['id_number'],
            'title': session['title'],
        })
    return jsonify({'logged_in': False}), 401


# ---------------- Shared Helpers ----------------
ALLOWED_IMG = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
FACULTY_COLS = "id, employee_id, full_name, designation, gender, email, phone, dob, qualification, photo"
STUDENT_COLS = ("id, registration_no, full_name, course, semester, course_sem, "
                "gender, dob, father_name, mother_name, phone, email, aadhaar, photo")
COURSE_COLS = "id, course_code, course_name, duration, total_semesters"
SUBJECT_COLS = "id, course_code, semester, subject_code, subject_name, credits, duration, subject_type"


def get_payload():
    """Accept both multipart/form-data and JSON."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


def field(data, key, default=''):
    return str(data.get(key) or default).strip()


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_date(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def time_sort_key(label):
    """'09:00 AM - 10:00 AM' -> minutes since midnight of the start time."""
    m = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)', label or '', re.I)
    if not m:
        return 9999
    hour = int(m.group(1)) % 12
    if m.group(3).upper() == 'PM':
        hour += 12
    return hour * 60 + int(m.group(2))


def save_photo(identifier, prefix='fac'):
    """Saves an uploaded photo and returns its URL path, or None."""
    file = request.files.get('photo')
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_IMG:
        return None
    upload_dir = os.path.join(BASE_DIR, 'static', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    filename = secure_filename(f"{prefix}_{identifier}_{file.filename}")
    file.save(os.path.join(upload_dir, filename))
    return f"/static/uploads/{filename}"


def serialize_rows(rows):
    for row in rows:
        for k, v in row.items():
            if isinstance(v, (date, datetime)):
                row[k] = v.strftime('%Y-%m-%d')
    return rows


def unauthorized():
    return jsonify({'success': False, 'message': 'Unauthorized'}), 401


def db_error(err):
    return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


def fail(message, status=400):
    return jsonify({'success': False, 'message': message}), status


def get_course(cursor, course_code):
    """Return the course row (dict cursor required) or None."""
    cursor.execute(f"SELECT {COURSE_COLS} FROM courses WHERE course_code = %s", (course_code,))
    return cursor.fetchone()


def current_faculty_id(cursor):
    cursor.execute("SELECT id FROM faculty WHERE employee_id = %s", (session.get('id_number'),))
    row = cursor.fetchone()
    return row['id'] if row else None


def faculty_subject(cursor, faculty_id, subject_id):
    """Return the subject row only if it is assigned to this faculty."""
    cursor.execute("""
        SELECT s.id, s.course_code, s.semester, s.subject_code, s.subject_name
        FROM subjects s JOIN faculty_assignments fa ON fa.subject_id = s.id
        WHERE s.id = %s AND fa.faculty_id = %s
    """, (subject_id, faculty_id))
    return cursor.fetchone()


# ---------------- Faculty CRUD API Routes (Admin) ----------------
@app.route('/api/faculty', methods=['GET'])
def get_faculty():
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT {FACULTY_COLS} FROM faculty ORDER BY id DESC")
        rows = serialize_rows(cursor.fetchall())
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'faculty': rows})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/faculty', methods=['POST'])
def add_faculty():
    if session.get('role') != 'admin':
        return unauthorized()

    data = get_payload()
    employee_id   = field(data, 'employee_id')
    password      = field(data, 'password')
    full_name     = field(data, 'full_name')
    designation   = field(data, 'designation', 'Professor')
    gender        = field(data, 'gender')
    email         = field(data, 'email')
    phone         = field(data, 'phone')
    dob           = field(data, 'dob') or None
    qualification = field(data, 'qualification')

    if not employee_id or not password or not full_name or not email:
        return jsonify({'success': False,
                        'message': 'Employee ID, Password, Full Name and Email are required!'}), 400

    photo_path = save_photo(employee_id, 'fac') or ''

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO faculty (employee_id, password, full_name, designation, gender,
                                 email, phone, dob, qualification, photo)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (employee_id, password, full_name, designation, gender,
              email, phone, dob, qualification, photo_path))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Faculty added successfully!'})
    except mysql.connector.IntegrityError as err:
        if err.errno == 1062:
            return jsonify({'success': False,
                            'message': f'Employee ID "{employee_id}" already exists.'}), 409
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/faculty/<int:fac_id>', methods=['PUT'])
def update_faculty(fac_id):
    if session.get('role') != 'admin':
        return unauthorized()

    data = get_payload()
    full_name     = field(data, 'full_name')
    designation   = field(data, 'designation', 'Professor')
    gender        = field(data, 'gender')
    email         = field(data, 'email')
    phone         = field(data, 'phone')
    dob           = field(data, 'dob') or None
    qualification = field(data, 'qualification')
    password      = field(data, 'password')

    if not full_name or not email:
        return jsonify({'success': False, 'message': 'Full Name and Email are required!'}), 400

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM faculty WHERE id = %s", (fac_id,))
        fac = cursor.fetchone()
        if not fac:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Faculty not found'}), 404

        pwd_to_save = password if password else fac['password']
        photo_path = save_photo(fac['employee_id'], 'fac') or fac['photo']

        cursor.execute("""
            UPDATE faculty SET full_name=%s, designation=%s, gender=%s, email=%s, phone=%s,
                               dob=%s, qualification=%s, password=%s, photo=%s
            WHERE id=%s
        """, (full_name, designation, gender, email, phone, dob,
              qualification, pwd_to_save, photo_path, fac_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Faculty updated successfully!'})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/faculty/<int:fac_id>', methods=['DELETE'])
def delete_faculty(fac_id):
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM faculty WHERE id = %s", (fac_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Faculty deleted successfully!'})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


# ---------------- Course CRUD API Routes (Admin) ----------------
@app.route('/api/courses', methods=['GET'])
def get_courses():
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT {COURSE_COLS} FROM courses ORDER BY course_code")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'courses': rows})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


def parse_course_payload(data):
    """Returns (values, error_response). values = (code, name, duration, total_semesters)."""
    course_code = field(data, 'course_code')
    course_name = field(data, 'course_name')
    duration = field(data, 'duration', '3 Years')
    try:
        total_semesters = int(field(data, 'total_semesters', '6'))
    except ValueError:
        total_semesters = 0

    if not course_code or not course_name:
        return None, (jsonify({'success': False,
                               'message': 'Course Code and Course Name are required!'}), 400)
    if total_semesters < 1 or total_semesters > 12:
        return None, (jsonify({'success': False,
                               'message': 'Total semesters must be between 1 and 12.'}), 400)
    return (course_code, course_name, duration, total_semesters), None


@app.route('/api/courses', methods=['POST'])
def add_course():
    if session.get('role') != 'admin':
        return unauthorized()

    values, error = parse_course_payload(get_payload())
    if error:
        return error
    course_code, course_name, duration, total_semesters = values

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO courses (course_code, course_name, duration, total_semesters)
            VALUES (%s,%s,%s,%s)
        """, (course_code, course_name, duration, total_semesters))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': f'Course "{course_code}" created successfully!'})
    except mysql.connector.IntegrityError as err:
        if err.errno == 1062:
            return jsonify({'success': False,
                            'message': f'Course code "{course_code}" already exists.'}), 409
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/courses/<int:course_id>', methods=['PUT'])
def update_course(course_id):
    if session.get('role') != 'admin':
        return unauthorized()

    values, error = parse_course_payload(get_payload())
    if error:
        return error
    new_code, course_name, duration, total_semesters = values

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT {COURSE_COLS} FROM courses WHERE id = %s", (course_id,))
        course = cursor.fetchone()
        if not course:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Course not found'}), 404

        old_code = course['course_code']

        # Do not allow shrinking below semesters that are already in use
        cursor.execute("SELECT MAX(semester) AS m FROM students WHERE course = %s", (old_code,))
        max_student_sem = cursor.fetchone()['m'] or 0
        cursor.execute("SELECT MAX(semester) AS m FROM subjects WHERE course_code = %s", (old_code,))
        max_subject_sem = cursor.fetchone()['m'] or 0
        in_use = max(max_student_sem, max_subject_sem)
        if total_semesters < in_use:
            cursor.close()
            conn.close()
            return jsonify({'success': False,
                            'message': f'Cannot reduce to {total_semesters} semesters: students/subjects '
                                       f'already exist in Semester {in_use}.'}), 400

        # Subjects / timetable slots follow the new code automatically (ON UPDATE CASCADE)
        cursor.execute("""
            UPDATE courses SET course_code=%s, course_name=%s, duration=%s, total_semesters=%s
            WHERE id=%s
        """, (new_code, course_name, duration, total_semesters, course_id))

        # Students store the course code as text, so keep them in sync
        if new_code != old_code:
            cursor.execute("""
                UPDATE students SET course=%s, course_sem=CONCAT(%s, ' - Sem ', semester)
                WHERE course=%s
            """, (new_code, new_code, old_code))

        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Course updated successfully!'})
    except mysql.connector.IntegrityError as err:
        if err.errno == 1062:
            return jsonify({'success': False,
                            'message': f'Course code "{new_code}" already exists.'}), 409
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/courses/<int:course_id>', methods=['DELETE'])
def delete_course(course_id):
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT {COURSE_COLS} FROM courses WHERE id = %s", (course_id,))
        course = cursor.fetchone()
        if not course:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Course not found'}), 404

        cursor.execute("SELECT COUNT(*) AS c FROM students WHERE course = %s", (course['course_code'],))
        student_count = cursor.fetchone()['c']
        if student_count > 0:
            cursor.close()
            conn.close()
            return jsonify({'success': False,
                            'message': f'Cannot delete "{course["course_code"]}": {student_count} student(s) '
                                       f'are enrolled. Move or delete them first.'}), 409

        # Subjects, slots, timetable entries and allotments are removed automatically (CASCADE)
        cursor.execute("DELETE FROM courses WHERE id = %s", (course_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Course deleted successfully!'})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


# ---------------- Subject CRUD API Routes (Admin) ----------------
@app.route('/api/subjects', methods=['GET'])
def get_subjects():
    if session.get('role') != 'admin':
        return unauthorized()
    course_code = request.args.get('course', '').strip()
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        if course_code:
            cursor.execute(f"SELECT {SUBJECT_COLS} FROM subjects WHERE course_code=%s "
                           f"ORDER BY semester, subject_code", (course_code,))
        else:
            cursor.execute(f"SELECT {SUBJECT_COLS} FROM subjects ORDER BY course_code, semester, subject_code")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'subjects': rows})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


def parse_subject_payload(data):
    subject_code = field(data, 'subject_code')
    subject_name = field(data, 'subject_name')
    duration = field(data, 'duration')
    subject_type = field(data, 'subject_type', 'Theory')
    try:
        credits = int(field(data, 'credits', '4'))
    except ValueError:
        credits = 0

    if not subject_code or not subject_name or not duration:
        return None, (jsonify({'success': False,
                               'message': 'Subject Code, Name and Duration are required!'}), 400)
    if credits < 1 or credits > 10:
        return None, (jsonify({'success': False,
                               'message': 'Credits must be between 1 and 10.'}), 400)
    if subject_type not in ('Theory', 'Practical'):
        subject_type = 'Theory'
    return (subject_code, subject_name, credits, duration, subject_type), None


@app.route('/api/subjects', methods=['POST'])
def add_subject():
    if session.get('role') != 'admin':
        return unauthorized()

    data = get_payload()
    course_code = field(data, 'course_code')
    try:
        semester = int(field(data, 'semester'))
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid semester.'}), 400

    values, error = parse_subject_payload(data)
    if error:
        return error
    subject_code, subject_name, credits, duration, subject_type = values

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        course = get_course(cursor, course_code)
        if not course:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Selected course does not exist.'}), 404
        if semester < 1 or semester > course['total_semesters']:
            cursor.close()
            conn.close()
            return jsonify({'success': False,
                            'message': f'{course_code} has only {course["total_semesters"]} semesters.'}), 400

        cursor.execute("""
            INSERT INTO subjects (course_code, semester, subject_code, subject_name,
                                  credits, duration, subject_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (course_code, semester, subject_code, subject_name, credits, duration, subject_type))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Subject added successfully!'})
    except mysql.connector.IntegrityError as err:
        if err.errno == 1062:
            return jsonify({'success': False,
                            'message': f'Subject code "{subject_code}" already exists in {course_code}.'}), 409
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/subjects/<int:subject_id>', methods=['PUT'])
def update_subject(subject_id):
    if session.get('role') != 'admin':
        return unauthorized()

    values, error = parse_subject_payload(get_payload())
    if error:
        return error
    subject_code, subject_name, credits, duration, subject_type = values

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, course_code FROM subjects WHERE id = %s", (subject_id,))
        sub = cursor.fetchone()
        if not sub:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Subject not found'}), 404

        cursor.execute("""
            UPDATE subjects SET subject_code=%s, subject_name=%s, credits=%s,
                                duration=%s, subject_type=%s
            WHERE id=%s
        """, (subject_code, subject_name, credits, duration, subject_type, subject_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Subject updated successfully!'})
    except mysql.connector.IntegrityError as err:
        if err.errno == 1062:
            return jsonify({'success': False,
                            'message': f'Subject code "{subject_code}" already exists in this course.'}), 409
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/subjects/<int:subject_id>', methods=['DELETE'])
def delete_subject(subject_id):
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM subjects WHERE id = %s", (subject_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Subject deleted successfully!'})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


# ---------------- Student CRUD & Promotion API Routes (Admin) ----------------
@app.route('/api/students', methods=['GET'])
def get_students():
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT {STUDENT_COLS} FROM students ORDER BY id DESC")
        rows = serialize_rows(cursor.fetchall())
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'students': rows})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


def validate_student_course(cursor, course, semester):
    """Returns an error (response, status) if the course/semester is invalid, else None."""
    course_row = get_course(cursor, course)
    if not course_row:
        return (jsonify({'success': False,
                         'message': 'Selected course does not exist. Create it in Course Catalog first.'}), 400)
    if semester < 1 or semester > course_row['total_semesters']:
        return (jsonify({'success': False,
                         'message': f'{course} has only {course_row["total_semesters"]} semesters.'}), 400)
    return None


@app.route('/api/students', methods=['POST'])
def add_student():
    if session.get('role') != 'admin':
        return unauthorized()

    data = get_payload()
    registration_no = field(data, 'registration_no')
    password        = field(data, 'password')
    full_name       = field(data, 'full_name')
    course          = field(data, 'course')
    try:
        semester = int(field(data, 'semester', '1'))
    except ValueError:
        semester = 1
    course_sem  = f"{course} - Sem {semester}"
    gender      = field(data, 'gender')
    dob         = field(data, 'dob') or None
    father_name = field(data, 'father_name')
    mother_name = field(data, 'mother_name')
    phone       = field(data, 'phone')
    email       = field(data, 'email')
    aadhaar     = field(data, 'aadhaar')

    if not registration_no or not password or not full_name:
        return jsonify({'success': False,
                        'message': 'Registration No, Password, and Full Name are required!'}), 400
    if not course:
        return jsonify({'success': False, 'message': 'Please select a course.'}), 400

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        invalid = validate_student_course(cursor, course, semester)
        if invalid:
            cursor.close()
            conn.close()
            return invalid

        photo_path = save_photo(registration_no, 'stu') or ''

        cursor.execute("""
            INSERT INTO students (registration_no, password, full_name, course, semester,
                                  course_sem, gender, dob, father_name, mother_name,
                                  phone, email, aadhaar, photo)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (registration_no, password, full_name, course, semester, course_sem,
              gender, dob, father_name, mother_name, phone, email, aadhaar, photo_path))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Student registered successfully!'})
    except mysql.connector.IntegrityError as err:
        if err.errno == 1062:
            return jsonify({'success': False,
                            'message': f'Registration No "{registration_no}" already exists.'}), 409
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/students/<int:stu_id>', methods=['PUT'])
def update_student(stu_id):
    if session.get('role') != 'admin':
        return unauthorized()

    data = get_payload()
    full_name   = field(data, 'full_name')
    course      = field(data, 'course')
    try:
        semester = int(field(data, 'semester', '1'))
    except ValueError:
        semester = 1
    course_sem  = f"{course} - Sem {semester}"
    gender      = field(data, 'gender')
    dob         = field(data, 'dob') or None
    father_name = field(data, 'father_name')
    mother_name = field(data, 'mother_name')
    phone       = field(data, 'phone')
    email       = field(data, 'email')
    aadhaar     = field(data, 'aadhaar')
    password    = field(data, 'password')

    if not full_name:
        return jsonify({'success': False, 'message': 'Full Name is required!'}), 400
    if not course:
        return jsonify({'success': False, 'message': 'Please select a course.'}), 400

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM students WHERE id = %s", (stu_id,))
        stu = cursor.fetchone()
        if not stu:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Student not found'}), 404

        invalid = validate_student_course(cursor, course, semester)
        if invalid:
            cursor.close()
            conn.close()
            return invalid

        pwd_to_save = password if password else stu['password']
        photo_path = save_photo(stu['registration_no'], 'stu') or stu['photo']

        cursor.execute("""
            UPDATE students SET full_name=%s, course=%s, semester=%s, course_sem=%s,
                                gender=%s, dob=%s, father_name=%s, mother_name=%s,
                                phone=%s, email=%s, aadhaar=%s, password=%s, photo=%s
            WHERE id=%s
        """, (full_name, course, semester, course_sem, gender, dob,
              father_name, mother_name, phone, email, aadhaar, pwd_to_save, photo_path, stu_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Student updated successfully!'})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/students/<int:stu_id>', methods=['DELETE'])
def delete_student(stu_id):
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM students WHERE id = %s", (stu_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Student deleted successfully!'})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/students/<int:stu_id>/promote', methods=['POST'])
def promote_student(stu_id):
    if session.get('role') != 'admin':
        return unauthorized()
    data = get_payload()
    try:
        max_sems = int(field(data, 'max_sems', '8'))
    except ValueError:
        max_sems = 8

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, full_name, course, semester FROM students WHERE id = %s", (stu_id,))
        stu = cursor.fetchone()
        if not stu:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Student not found'}), 404

        # The course in the database decides the final semester
        course_row = get_course(cursor, stu['course'])
        if course_row:
            max_sems = course_row['total_semesters']

        if stu['semester'] >= max_sems:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Student is already in the final semester!'}), 400

        new_sem = stu['semester'] + 1
        new_course_sem = f"{stu['course']} - Sem {new_sem}"
        cursor.execute("UPDATE students SET semester=%s, course_sem=%s WHERE id=%s",
                       (new_sem, new_course_sem, stu_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True,
                        'message': f"{stu['full_name']} promoted to Semester {new_sem}!"})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


@app.route('/api/students/promote-semester', methods=['POST'])
def promote_entire_semester():
    if session.get('role') != 'admin':
        return unauthorized()
    data = get_payload()
    course = field(data, 'course')
    try:
        semester = int(field(data, 'semester'))
        max_sems = int(field(data, 'max_sems', '8'))
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid semester value'}), 400

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        course_row = get_course(cursor, course)
        if course_row:
            max_sems = course_row['total_semesters']

        if semester >= max_sems:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Cannot promote beyond the final semester.'}), 400

        new_sem = semester + 1
        new_course_sem = f"{course} - Sem {new_sem}"
        cursor.execute("""
            UPDATE students SET semester=%s, course_sem=%s
            WHERE course=%s AND semester=%s
        """, (new_sem, new_course_sem, course, semester))
        promoted_count = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True,
                        'message': f"Promoted {promoted_count} student(s) to Semester {new_sem}!"})
    except mysql.connector.Error as err:
        return jsonify({'success': False, 'message': f'Database error: {err}'}), 500


# =====================================================================
#  Faculty Allotment (Admin)  - which faculty teaches which subject
# =====================================================================
def allotment_conflict(cur, subject_id, faculty_id):
    """A faculty can't be given a subject whose timetable slots clash with their other subjects."""
    cur.execute("""
        SELECT e.day_name, t.time_label FROM timetable_entries e
        JOIN timetable_slots t ON t.id = e.slot_id WHERE e.subject_id = %s
    """, (subject_id,))
    mine = {(r['day_name'], r['time_label']) for r in cur.fetchall()}
    if not mine:
        return None
    cur.execute("""
        SELECT e.day_name, t.time_label, s.subject_code, t.course_code, t.semester
        FROM timetable_entries e
        JOIN timetable_slots t ON t.id = e.slot_id
        JOIN subjects s ON s.id = e.subject_id
        JOIN faculty_assignments fa ON fa.subject_id = s.id
        WHERE fa.faculty_id = %s AND s.id <> %s
    """, (faculty_id, subject_id))
    for r in cur.fetchall():
        if (r['day_name'], r['time_label']) in mine:
            return (f"Time clash: this faculty already teaches {r['subject_code']} "
                    f"({r['course_code']} Sem {r['semester']}) on {r['day_name']} at {r['time_label']}.")
    return None


@app.route('/api/assignments', methods=['GET'])
def get_assignments():
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id, employee_id, full_name, designation FROM faculty ORDER BY full_name")
            faculty = cur.fetchall()
            cur.execute("""
                SELECT s.id, s.course_code, s.semester, s.subject_code, s.subject_name, s.subject_type,
                       fa.faculty_id, f.full_name AS faculty_name
                FROM subjects s
                LEFT JOIN faculty_assignments fa ON fa.subject_id = s.id
                LEFT JOIN faculty f ON f.id = fa.faculty_id
                ORDER BY s.course_code, s.semester, s.subject_code
            """)
            subjects = cur.fetchall()
        return jsonify({'success': True, 'faculty': faculty, 'subjects': subjects})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/assignments/<int:subject_id>', methods=['PUT'])
def set_assignment(subject_id):
    if session.get('role') != 'admin':
        return unauthorized()
    faculty_id = to_int(get_payload().get('faculty_id'))
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("SELECT id FROM subjects WHERE id = %s", (subject_id,))
            if not cur.fetchone():
                return fail('Subject not found', 404)

            if faculty_id is None:
                cur.execute("DELETE FROM faculty_assignments WHERE subject_id = %s", (subject_id,))
                return jsonify({'success': True, 'message': 'Faculty unassigned from subject.'})

            cur.execute("SELECT full_name FROM faculty WHERE id = %s", (faculty_id,))
            fac = cur.fetchone()
            if not fac:
                return fail('Faculty not found', 404)

            clash = allotment_conflict(cur, subject_id, faculty_id)
            if clash:
                return fail(clash, 409)

            cur.execute("""
                INSERT INTO faculty_assignments (subject_id, faculty_id) VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE faculty_id = VALUES(faculty_id)
            """, (subject_id, faculty_id))
            return jsonify({'success': True, 'message': f"Subject assigned to {fac['full_name']}.",
                            'faculty_name': fac['full_name']})
    except mysql.connector.Error as err:
        return db_error(err)


# =====================================================================
#  Master Timetable (Admin)
# =====================================================================
def faculty_conflict(cur, subject_id, day, time_label, exclude_slot_id):
    """Return an error message if the subject's faculty is already teaching at this day/time."""
    cur.execute("""
        SELECT fa.faculty_id, f.full_name FROM faculty_assignments fa
        JOIN faculty f ON f.id = fa.faculty_id WHERE fa.subject_id = %s
    """, (subject_id,))
    fa = cur.fetchone()
    if not fa:
        return None
    cur.execute("""
        SELECT t.course_code, t.semester, s.subject_code
        FROM timetable_entries e
        JOIN timetable_slots t ON t.id = e.slot_id
        JOIN subjects s ON s.id = e.subject_id
        JOIN faculty_assignments x ON x.subject_id = s.id
        WHERE x.faculty_id = %s AND e.day_name = %s AND t.time_label = %s AND e.slot_id <> %s
    """, (fa['faculty_id'], day, time_label, exclude_slot_id))
    row = cur.fetchone()
    if row:
        return (f"{fa['full_name']} is already teaching {row['subject_code']} "
                f"({row['course_code']} Sem {row['semester']}) on {day} at {time_label}.")
    return None


@app.route('/api/timetable', methods=['GET'])
def get_timetable():
    if session.get('role') != 'admin':
        return unauthorized()
    course = request.args.get('course', '').strip()
    semester = to_int(request.args.get('semester'))
    if not course or semester is None:
        return fail('Course and semester are required.')
    try:
        with db_cursor(commit=True) as cur:
            c = get_course(cur, course)
            if not c:
                return fail('Course not found', 404)
            if semester < 1 or semester > c['total_semesters']:
                return fail(f'{course} has only {c["total_semesters"]} semesters.')

            cur.execute("SELECT COUNT(*) AS n FROM timetable_slots WHERE course_code=%s AND semester=%s",
                        (course, semester))
            if cur.fetchone()['n'] == 0:
                for i, label in enumerate(DEFAULT_SLOTS, 1):
                    cur.execute("""INSERT INTO timetable_slots (course_code, semester, slot_order, time_label)
                                   VALUES (%s,%s,%s,%s)""", (course, semester, i, label))

            cur.execute("""SELECT id, slot_order, time_label FROM timetable_slots
                           WHERE course_code=%s AND semester=%s""", (course, semester))
            slots = cur.fetchall()
            slots.sort(key=lambda s: (time_sort_key(s['time_label']), s['slot_order'], s['id']))

            cur.execute("""
                SELECT e.id, e.slot_id, e.day_name, e.entry_type, e.subject_id, e.custom_text,
                       s.subject_code, s.subject_name, s.subject_type, f.full_name AS faculty_name
                FROM timetable_entries e
                JOIN timetable_slots t ON t.id = e.slot_id
                LEFT JOIN subjects s ON s.id = e.subject_id
                LEFT JOIN faculty_assignments fa ON fa.subject_id = s.id
                LEFT JOIN faculty f ON f.id = fa.faculty_id
                WHERE t.course_code = %s AND t.semester = %s
            """, (course, semester))
            entries = cur.fetchall()

            cur.execute("""
                SELECT s.id, s.subject_code, s.subject_name, s.subject_type, f.full_name AS faculty_name
                FROM subjects s
                LEFT JOIN faculty_assignments fa ON fa.subject_id = s.id
                LEFT JOIN faculty f ON f.id = fa.faculty_id
                WHERE s.course_code = %s AND s.semester = %s
                ORDER BY s.subject_code
            """, (course, semester))
            subjects = cur.fetchall()

        return jsonify({'success': True, 'slots': slots, 'entries': entries, 'subjects': subjects})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/timetable/slots', methods=['POST'])
def add_timetable_slot():
    if session.get('role') != 'admin':
        return unauthorized()
    data = get_payload()
    course = field(data, 'course_code')
    semester = to_int(data.get('semester'))
    label = field(data, 'time_label')
    if not course or semester is None or not label:
        return fail('Course, semester and time slot are required.')
    try:
        with db_cursor(commit=True) as cur:
            if not get_course(cur, course):
                return fail('Course not found', 404)
            cur.execute("""SELECT COALESCE(MAX(slot_order), 0) + 1 AS n FROM timetable_slots
                           WHERE course_code=%s AND semester=%s""", (course, semester))
            order = cur.fetchone()['n']
            cur.execute("""INSERT INTO timetable_slots (course_code, semester, slot_order, time_label)
                           VALUES (%s,%s,%s,%s)""", (course, semester, order, label))
        return jsonify({'success': True, 'message': 'Time slot added.'})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/timetable/slots/<int:slot_id>', methods=['PUT'])
def update_timetable_slot(slot_id):
    if session.get('role') != 'admin':
        return unauthorized()
    label = field(get_payload(), 'time_label')
    if not label:
        return fail('Time slot cannot be empty.')
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("SELECT id FROM timetable_slots WHERE id=%s", (slot_id,))
            if not cur.fetchone():
                return fail('Time slot not found', 404)
            # Changing the time must not create a clash for any faculty
            cur.execute("""SELECT day_name, subject_id FROM timetable_entries
                           WHERE slot_id=%s AND subject_id IS NOT NULL""", (slot_id,))
            for e in cur.fetchall():
                clash = faculty_conflict(cur, e['subject_id'], e['day_name'], label, slot_id)
                if clash:
                    return fail(clash, 409)
            cur.execute("UPDATE timetable_slots SET time_label=%s WHERE id=%s", (label, slot_id))
        return jsonify({'success': True, 'message': 'Time slot updated.'})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/timetable/slots/<int:slot_id>', methods=['DELETE'])
def delete_timetable_slot(slot_id):
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("DELETE FROM timetable_slots WHERE id=%s", (slot_id,))
        return jsonify({'success': True, 'message': 'Time slot removed.'})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/timetable/entry', methods=['PUT'])
def save_timetable_entry():
    """Set one cell. entry_type: subject | break | lunch | custom | empty."""
    if session.get('role') != 'admin':
        return unauthorized()
    data = get_payload()
    slot_id = to_int(data.get('slot_id'))
    day = field(data, 'day')
    etype = field(data, 'entry_type', 'empty')
    text = field(data, 'custom_text')
    subject_id = to_int(data.get('subject_id'))
    all_days = bool(data.get('apply_all_days'))

    if etype not in ('subject', 'break', 'lunch', 'custom', 'empty'):
        return fail('Invalid entry type.')
    if etype == 'subject':
        all_days = False          # a subject is always set for one specific day
    if not all_days and day not in DAYS:
        return fail('Invalid day.')
    if etype == 'subject' and subject_id is None:
        return fail('Please select a subject.')
    if etype == 'custom' and not text:
        return fail('Please type the text to show in this slot.')

    days = DAYS if all_days else [day]
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("SELECT id, course_code, semester, time_label FROM timetable_slots WHERE id=%s",
                        (slot_id,))
            slot = cur.fetchone()
            if not slot:
                return fail('Time slot not found', 404)

            if etype == 'empty':
                for d in days:
                    cur.execute("DELETE FROM timetable_entries WHERE slot_id=%s AND day_name=%s", (slot_id, d))
                return jsonify({'success': True, 'message': 'Slot cleared.'})

            if etype == 'subject':
                # Only subjects that belong to THIS course and semester are accepted
                cur.execute("""SELECT id FROM subjects WHERE id=%s AND course_code=%s AND semester=%s""",
                            (subject_id, slot['course_code'], slot['semester']))
                if not cur.fetchone():
                    return fail('That subject does not belong to this course and semester.')
                clash = faculty_conflict(cur, subject_id, day, slot['time_label'], slot_id)
                if clash:
                    return fail(clash, 409)
                text = ''
            else:
                subject_id = None
                if etype == 'break':
                    text = text or 'Break'
                elif etype == 'lunch':
                    text = text or 'Lunch Break'

            for d in days:
                cur.execute("""
                    INSERT INTO timetable_entries (slot_id, day_name, entry_type, subject_id, custom_text)
                    VALUES (%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE entry_type=VALUES(entry_type),
                        subject_id=VALUES(subject_id), custom_text=VALUES(custom_text)
                """, (slot_id, d, etype, subject_id, text))
        return jsonify({'success': True, 'message': 'Timetable updated.'})
    except mysql.connector.Error as err:
        return db_error(err)


# =====================================================================
#  Faculty - my subjects, my timetable
# =====================================================================
@app.route('/api/my/subjects', methods=['GET'])
def my_subjects():
    if session.get('role') != 'faculty':
        return unauthorized()
    try:
        with db_cursor() as cur:
            fid = current_faculty_id(cur)
            cur.execute("""
                SELECT s.id, s.course_code, s.semester, s.subject_code, s.subject_name, s.subject_type,
                       (SELECT COUNT(*) FROM students st
                         WHERE st.course = s.course_code AND st.semester = s.semester) AS student_count
                FROM faculty_assignments fa JOIN subjects s ON s.id = fa.subject_id
                WHERE fa.faculty_id = %s
                ORDER BY s.course_code, s.semester, s.subject_code
            """, (fid,))
            rows = cur.fetchall()
        return jsonify({'success': True, 'subjects': rows})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/my/timetable', methods=['GET'])
def my_timetable():
    if session.get('role') != 'faculty':
        return unauthorized()
    try:
        with db_cursor() as cur:
            fid = current_faculty_id(cur)
            cur.execute("""
                SELECT e.day_name, t.time_label, t.course_code, t.semester,
                       s.id AS subject_id, s.subject_code, s.subject_name, s.subject_type
                FROM timetable_entries e
                JOIN timetable_slots t ON t.id = e.slot_id
                JOIN subjects s ON s.id = e.subject_id
                JOIN faculty_assignments fa ON fa.subject_id = s.id
                WHERE fa.faculty_id = %s AND e.entry_type = 'subject'
            """, (fid,))
            rows = cur.fetchall()

            # Flag the class that is running right now (attendance window is open)
            now = datetime.now()
            minutes = now.hour * 60 + now.minute
            today = now.strftime('%A')
            for r in rows:
                rng = slot_range(r['time_label'])
                r['is_open'] = bool(r['day_name'] == today and rng and rng[0] <= minutes < rng[1])
        rows.sort(key=lambda r: (DAYS.index(r['day_name']) if r['day_name'] in DAYS else 9,
                                 time_sort_key(r['time_label'])))
        return jsonify({'success': True, 'today': datetime.now().strftime('%A'), 'timetable': rows})
    except mysql.connector.Error as err:
        return db_error(err)


# =====================================================================
#  Attendance (Faculty marks / exports, Admin views / exports)
#  Faculty can mark/change attendance ONLY for today and ONLY while the
#  current time is inside one of the subject's timetable slots.
# =====================================================================
def slot_range(label):
    """'09:00 AM - 10:00 AM' -> (start_minute, end_minute) or None."""
    times = re.findall(r'(\d{1,2}):(\d{2})\s*(AM|PM)', label or '', re.I)
    if len(times) < 2:
        return None

    def to_min(t):
        h = int(t[0]) % 12
        if t[2].upper() == 'PM':
            h += 12
        return h * 60 + int(t[1])

    return to_min(times[0]), to_min(times[1])


def open_slot(cur, subject_id):
    """Time label of this subject's class that is running right now, else None."""
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    cur.execute("""
        SELECT t.time_label FROM timetable_entries e
        JOIN timetable_slots t ON t.id = e.slot_id
        WHERE e.subject_id = %s AND e.entry_type = 'subject' AND e.day_name = %s
    """, (subject_id, now.strftime('%A')))
    for r in cur.fetchall():
        rng = slot_range(r['time_label'])
        if rng and rng[0] <= minutes < rng[1]:
            return r['time_label']
    return None


def today_slots(cur, subject_id):
    """All time labels of this subject's classes scheduled for today."""
    cur.execute("""
        SELECT t.time_label FROM timetable_entries e
        JOIN timetable_slots t ON t.id = e.slot_id
        WHERE e.subject_id = %s AND e.entry_type = 'subject' AND e.day_name = %s
    """, (subject_id, datetime.now().strftime('%A')))
    return sorted((r['time_label'] for r in cur.fetchall()), key=time_sort_key)


# =====================================================================
#  FACE ATTENDANCE LINK
#  The separate face-recognition system calls these two endpoints.
#  Both require the shared secret in the X-Face-Key header.
#    GET  /api/face/student/<registration_no>  -> cross-check at face registration
#    POST /api/face/mark                       -> mark Present when a face is detected
# =====================================================================
def face_key_ok():
    sent = request.headers.get('X-Face-Key', '')
    return hmac.compare_digest(sent, FACE_API_KEY)


def student_open_class(cur, course, semester):
    """The subject class running right now for this course + semester, else None."""
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    cur.execute("""
        SELECT e.subject_id, t.time_label, s.subject_code, s.subject_name
        FROM timetable_entries e
        JOIN timetable_slots t ON t.id = e.slot_id
        JOIN subjects s ON s.id = e.subject_id
        WHERE t.course_code = %s AND t.semester = %s
          AND e.entry_type = 'subject' AND e.day_name = %s
    """, (course, semester, now.strftime('%A')))
    for r in cur.fetchall():
        rng = slot_range(r['time_label'])
        if rng and rng[0] <= minutes < rng[1]:
            return r
    return None


@app.route('/api/face/student/<reg_no>', methods=['GET'])
def face_student(reg_no):
    """Used by the face system at registration to cross-check the registration number."""
    if not face_key_ok():
        return unauthorized()
    try:
        with db_cursor() as cur:
            cur.execute("""SELECT registration_no, full_name, course, semester
                           FROM students WHERE registration_no = %s""", (reg_no,))
            stu = cur.fetchone()
        if not stu:
            return fail('This registration number is not in the college portal.', 404)
        return jsonify({'success': True, 'student': stu})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/face/mark', methods=['POST'])
def face_mark():
    """Called by the face system when a face is recognised."""
    if not face_key_ok():
        return unauthorized()
    reg_no = field(get_payload(), 'registration_no')
    if not reg_no:
        return fail('Registration number required.')
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("""SELECT id, registration_no, full_name, course, semester
                           FROM students WHERE registration_no = %s""", (reg_no,))
            stu = cur.fetchone()
            if not stu:
                return jsonify({'success': False, 'status': 'unknown',
                                'message': f'{reg_no} is not in the portal'})

            cls = student_open_class(cur, stu['course'], stu['semester'])
            if not cls:
                return jsonify({'success': False, 'status': 'no_class',
                                'message': f"No class running now for {stu['course']} Sem {stu['semester']}"})

            label = f"{cls['subject_code']} - {cls['subject_name']}"
            today = date.today()

            cur.execute("""SELECT status FROM attendance
                           WHERE student_id=%s AND subject_id=%s AND att_date=%s""",
                        (stu['id'], cls['subject_id'], today))
            row = cur.fetchone()
            if row and row['status'] == 'Present':
                return jsonify({'success': True, 'status': 'already', 'subject': label,
                                'message': label})

            cur.execute("SELECT faculty_id FROM faculty_assignments WHERE subject_id=%s",
                        (cls['subject_id'],))
            fa = cur.fetchone()
            fid = fa['faculty_id'] if fa else None

            cur.execute("""
                INSERT INTO attendance (student_id, subject_id, faculty_id, att_date, status)
                VALUES (%s,%s,%s,%s,'Present')
                ON DUPLICATE KEY UPDATE status='Present',
                                        faculty_id=COALESCE(VALUES(faculty_id), faculty_id)
            """, (stu['id'], cls['subject_id'], fid, today))
            return jsonify({'success': True, 'status': 'marked', 'subject': label,
                            'name': stu['full_name'], 'message': label})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/my/roster', methods=['GET'])
def my_roster():
    if session.get('role') != 'faculty':
        return unauthorized()
    subject_id = to_int(request.args.get('subject_id'))
    att_date = parse_date(request.args.get('date'))
    if subject_id is None or not att_date:
        return fail('Subject and a valid date are required.')
    try:
        with db_cursor() as cur:
            fid = current_faculty_id(cur)
            sub = faculty_subject(cur, fid, subject_id)
            if not sub:
                return fail('This subject is not assigned to you.', 403)
            cur.execute("""
                SELECT st.id, st.registration_no, st.full_name, a.status
                FROM students st
                LEFT JOIN attendance a ON a.student_id = st.id AND a.subject_id = %s AND a.att_date = %s
                WHERE st.course = %s AND st.semester = %s
                ORDER BY st.registration_no
            """, (subject_id, att_date, sub['course_code'], sub['semester']))
            students = cur.fetchall()

            is_today = att_date == date.today()
            slot = open_slot(cur, subject_id) if is_today else None
            slots_today = today_slots(cur, subject_id)

        if slot:
            reason = ''
        elif not is_today:
            reason = 'Past/future dates are view-only. Attendance can only be marked today.'
        elif not slots_today:
            reason = 'This subject has no class scheduled today, so attendance is locked.'
        else:
            reason = ("Attendance is open only during the class time. Today's class: "
                      + ', '.join(slots_today))
        saved = any(s['status'] for s in students)
        return jsonify({'success': True, 'subject': sub, 'students': students, 'saved': saved,
                        'editable': bool(slot), 'slot': slot, 'reason': reason})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/my/attendance', methods=['POST'])
def save_attendance():
    if session.get('role') != 'faculty':
        return unauthorized()
    data = request.get_json(silent=True) or {}
    subject_id = to_int(data.get('subject_id'))
    att_date = parse_date(data.get('date'))
    records = data.get('records') or []
    if subject_id is None or not att_date:
        return fail('Subject and a valid date are required.')
    if att_date != date.today():
        return fail('Attendance can only be marked for today.', 403)
    if not records:
        return fail('No students to save.')
    try:
        with db_cursor(commit=True) as cur:
            fid = current_faculty_id(cur)
            sub = faculty_subject(cur, fid, subject_id)
            if not sub:
                return fail('This subject is not assigned to you.', 403)
            if not open_slot(cur, subject_id):
                return fail("Attendance is locked. It can only be marked during this "
                            "subject's scheduled class time on the timetable.", 403)
            cur.execute("SELECT id FROM students WHERE course=%s AND semester=%s",
                        (sub['course_code'], sub['semester']))
            valid_ids = {r['id'] for r in cur.fetchall()}

            rows = []
            for r in records:
                sid = to_int(r.get('student_id'))
                status = r.get('status')
                if sid in valid_ids and status in ('Present', 'Absent'):
                    rows.append((sid, subject_id, fid, att_date, status))
            if not rows:
                return fail('No valid attendance records received.')

            cur.executemany("""
                INSERT INTO attendance (student_id, subject_id, faculty_id, att_date, status)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status = VALUES(status), faculty_id = VALUES(faculty_id)
            """, rows)
        return jsonify({'success': True,
                        'message': f'Attendance saved for {len(rows)} student(s) on {att_date.strftime("%d-%m-%Y")}.'})
    except mysql.connector.Error as err:
        return db_error(err)


def build_attendance_csv(cur, subject_id, d_from, d_to):
    """Returns (csv_text, filename) or (None, message)."""
    cur.execute("SELECT id, course_code, semester, subject_code, subject_name FROM subjects WHERE id=%s",
                (subject_id,))
    sub = cur.fetchone()
    if not sub:
        return None, 'Subject not found.'

    where, params = "subject_id = %s", [subject_id]
    if d_from:
        where += " AND att_date >= %s"
        params.append(d_from)
    if d_to:
        where += " AND att_date <= %s"
        params.append(d_to)
    cur.execute(f"SELECT student_id, att_date, status FROM attendance WHERE {where} ORDER BY att_date", params)
    rows = cur.fetchall()
    if not rows:
        return None, 'No attendance records found for the selected subject and dates.'

    dates = sorted({r['att_date'] for r in rows})
    marks = {(r['student_id'], r['att_date']): r['status'] for r in rows}
    ids = sorted({r['student_id'] for r in rows})
    cur.execute(f"SELECT id, registration_no, full_name FROM students WHERE id IN ({','.join(['%s'] * len(ids))}) "
                f"ORDER BY registration_no", ids)
    students = cur.fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Subject', f"{sub['subject_code']} - {sub['subject_name']}"])
    w.writerow(['Course / Semester', f"{sub['course_code']} / Semester {sub['semester']}"])
    w.writerow(['Period', f"{dates[0].strftime('%d-%m-%Y')} to {dates[-1].strftime('%d-%m-%Y')}"])
    w.writerow(['Generated', datetime.now().strftime('%d-%m-%Y %H:%M')])
    w.writerow([])
    w.writerow(['Reg No', 'Name'] + [d.strftime('%d-%m-%Y') for d in dates] +
               ['Classes Held', 'Present', 'Absent', 'Attendance %'])
    for s in students:
        line, present, total = [], 0, 0
        for d in dates:
            st = marks.get((s['id'], d))
            if st == 'Present':
                present += 1
                total += 1
                line.append('P')
            elif st == 'Absent':
                total += 1
                line.append('A')
            else:
                line.append('-')
        pct = round(present * 100 / total, 2) if total else 0
        w.writerow([s['registration_no'], s['full_name']] + line + [total, present, total - present, pct])

    filename = secure_filename(f"attendance_{sub['course_code']}_sem{sub['semester']}_{sub['subject_code']}.csv")
    return buf.getvalue(), filename


def csv_response(text, filename):
    resp = Response('\ufeff' + text, mimetype='text/csv; charset=utf-8')   # BOM so Excel reads UTF-8
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@app.route('/api/my/attendance/export', methods=['GET'])
def export_my_attendance():
    if session.get('role') != 'faculty':
        return unauthorized()
    subject_id = to_int(request.args.get('subject_id'))
    if subject_id is None:
        return fail('Please select a subject.')
    try:
        with db_cursor() as cur:
            fid = current_faculty_id(cur)
            if not faculty_subject(cur, fid, subject_id):
                return fail('This subject is not assigned to you.', 403)
            text, name = build_attendance_csv(cur, subject_id,
                                              parse_date(request.args.get('from')),
                                              parse_date(request.args.get('to')))
        if text is None:
            return fail(name, 404)
        return csv_response(text, name)
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/admin/attendance', methods=['GET'])
def admin_attendance():
    """Admin: VIEW ONLY. There is no admin route that creates or edits attendance."""
    if session.get('role') != 'admin':
        return unauthorized()
    where, params = ["1=1"], []
    course = request.args.get('course', '').strip()
    semester = to_int(request.args.get('semester'))
    subject_id = to_int(request.args.get('subject_id'))
    d_from, d_to = parse_date(request.args.get('from')), parse_date(request.args.get('to'))
    if course:
        where.append("sub.course_code = %s")
        params.append(course)
    if semester is not None:
        where.append("sub.semester = %s")
        params.append(semester)
    if subject_id is not None:
        where.append("a.subject_id = %s")
        params.append(subject_id)
    if d_from:
        where.append("a.att_date >= %s")
        params.append(d_from)
    if d_to:
        where.append("a.att_date <= %s")
        params.append(d_to)
    clause = " AND ".join(where)
    try:
        with db_cursor() as cur:
            cur.execute(f"""
                SELECT st.registration_no, st.full_name, sub.course_code, sub.semester,
                       sub.subject_code, sub.subject_name,
                       COUNT(*) AS total, SUM(a.status = 'Present') AS present
                FROM attendance a
                JOIN students st ON st.id = a.student_id
                JOIN subjects sub ON sub.id = a.subject_id
                WHERE {clause}
                GROUP BY st.id, sub.id
                ORDER BY sub.course_code, sub.semester, sub.subject_code, st.registration_no
            """, params)
            summary = cur.fetchall()
            for r in summary:
                r['total'] = int(r['total'])
                r['present'] = int(r['present'] or 0)
                r['absent'] = r['total'] - r['present']
                r['percentage'] = round(r['present'] * 100 / r['total'], 1) if r['total'] else 0

            cur.execute(f"""
                SELECT a.att_date, a.status, st.registration_no, st.full_name,
                       sub.course_code, sub.semester, sub.subject_code, sub.subject_name,
                       f.full_name AS faculty_name
                FROM attendance a
                JOIN students st ON st.id = a.student_id
                JOIN subjects sub ON sub.id = a.subject_id
                LEFT JOIN faculty f ON f.id = a.faculty_id
                WHERE {clause}
                ORDER BY a.att_date DESC, sub.subject_code, st.registration_no
                LIMIT 1000
            """, params)
            records = serialize_rows(cur.fetchall())
        return jsonify({'success': True, 'summary': summary, 'records': records})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/admin/attendance/export', methods=['GET'])
def admin_export_attendance():
    if session.get('role') != 'admin':
        return unauthorized()
    subject_id = to_int(request.args.get('subject_id'))
    if subject_id is None:
        return fail('Please select a subject to export.')
    try:
        with db_cursor() as cur:
            text, name = build_attendance_csv(cur, subject_id,
                                              parse_date(request.args.get('from')),
                                              parse_date(request.args.get('to')))
        if text is None:
            return fail(name, 404)
        return csv_response(text, name)
    except mysql.connector.Error as err:
        return db_error(err)


# =====================================================================
#  Grades & Marks (Faculty)
# =====================================================================
ASSESSMENT_CATEGORIES = ('Internal Exam', 'Quiz', 'Assignment', 'Activity',
                         'Case Study', 'Semester Exam', 'Other')


def parse_assessment(data):
    """Returns ((title, category, max_marks), None) or (None, error_response)."""
    title = field(data, 'title')
    category = field(data, 'category', 'Internal Exam')
    try:
        max_marks = float(field(data, 'max_marks'))
    except ValueError:
        max_marks = 0
    if not title:
        return None, fail('Title is required.')
    if category not in ASSESSMENT_CATEGORIES:
        category = 'Other'
    if not (0 < max_marks <= 1000):
        return None, fail('Maximum marks must be between 1 and 1000.')
    return (title, category, round(max_marks, 2)), None


def assessment_for_faculty(cur, faculty_id, assessment_id):
    """Return the assessment row only if its subject is assigned to this faculty."""
    cur.execute("""
        SELECT a.id, a.subject_id, a.max_marks FROM assessments a
        JOIN faculty_assignments fa ON fa.subject_id = a.subject_id
        WHERE a.id = %s AND fa.faculty_id = %s
    """, (assessment_id, faculty_id))
    return cur.fetchone()


@app.route('/api/my/grades/sheet', methods=['GET'])
def grades_sheet():
    if session.get('role') != 'faculty':
        return unauthorized()
    subject_id = to_int(request.args.get('subject_id'))
    if subject_id is None:
        return fail('Please select a subject.')
    try:
        with db_cursor() as cur:
            fid = current_faculty_id(cur)
            sub = faculty_subject(cur, fid, subject_id)
            if not sub:
                return fail('This subject is not assigned to you.', 403)
            cur.execute("""SELECT id, registration_no, full_name FROM students
                           WHERE course=%s AND semester=%s ORDER BY registration_no""",
                        (sub['course_code'], sub['semester']))
            students = cur.fetchall()
            cur.execute("""SELECT id, title, category, max_marks FROM assessments
                           WHERE subject_id=%s ORDER BY id""", (subject_id,))
            assessments = cur.fetchall()
            cur.execute("""SELECT m.assessment_id, m.student_id, m.marks
                           FROM assessment_marks m JOIN assessments a ON a.id = m.assessment_id
                           WHERE a.subject_id=%s""", (subject_id,))
            marks = cur.fetchall()
        for a in assessments:
            a['max_marks'] = float(a['max_marks'])
        for m in marks:
            m['marks'] = float(m['marks'])
        return jsonify({'success': True, 'subject': sub, 'students': students,
                        'assessments': assessments, 'marks': marks})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/my/grades/assessments', methods=['POST'])
def add_assessment():
    if session.get('role') != 'faculty':
        return unauthorized()
    data = request.get_json(silent=True) or {}
    subject_id = to_int(data.get('subject_id'))
    values, error = parse_assessment(data)
    if error:
        return error
    title, category, max_marks = values
    try:
        with db_cursor(commit=True) as cur:
            fid = current_faculty_id(cur)
            if subject_id is None or not faculty_subject(cur, fid, subject_id):
                return fail('This subject is not assigned to you.', 403)
            cur.execute("""INSERT INTO assessments (subject_id, title, category, max_marks)
                           VALUES (%s,%s,%s,%s)""", (subject_id, title, category, max_marks))
        return jsonify({'success': True, 'message': f'"{title}" added.'})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/my/grades/assessments/<int:assessment_id>', methods=['PUT'])
def update_assessment(assessment_id):
    if session.get('role') != 'faculty':
        return unauthorized()
    values, error = parse_assessment(request.get_json(silent=True) or {})
    if error:
        return error
    title, category, max_marks = values
    try:
        with db_cursor(commit=True) as cur:
            fid = current_faculty_id(cur)
            if not assessment_for_faculty(cur, fid, assessment_id):
                return fail('Assessment not found.', 404)
            cur.execute("SELECT COUNT(*) AS n FROM assessment_marks WHERE assessment_id=%s AND marks > %s",
                        (assessment_id, max_marks))
            if cur.fetchone()['n'] > 0:
                return fail('Some students already have marks above this maximum. '
                            'Correct those marks first.', 409)
            cur.execute("""UPDATE assessments SET title=%s, category=%s, max_marks=%s WHERE id=%s""",
                        (title, category, max_marks, assessment_id))
        return jsonify({'success': True, 'message': 'Assessment updated.'})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/my/grades/assessments/<int:assessment_id>', methods=['DELETE'])
def delete_assessment(assessment_id):
    if session.get('role') != 'faculty':
        return unauthorized()
    try:
        with db_cursor(commit=True) as cur:
            fid = current_faculty_id(cur)
            if not assessment_for_faculty(cur, fid, assessment_id):
                return fail('Assessment not found.', 404)
            cur.execute("DELETE FROM assessments WHERE id=%s", (assessment_id,))   # marks cascade
        return jsonify({'success': True, 'message': 'Assessment deleted.'})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/my/grades/marks', methods=['POST'])
def save_marks():
    """entries: [{assessment_id, student_id, marks}]  - blank/null marks clears that entry."""
    if session.get('role') != 'faculty':
        return unauthorized()
    data = request.get_json(silent=True) or {}
    subject_id = to_int(data.get('subject_id'))
    entries = data.get('entries') or []
    if subject_id is None or not entries:
        return fail('Nothing to save.')
    try:
        with db_cursor(commit=True) as cur:
            fid = current_faculty_id(cur)
            sub = faculty_subject(cur, fid, subject_id)
            if not sub:
                return fail('This subject is not assigned to you.', 403)

            cur.execute("SELECT id, max_marks FROM assessments WHERE subject_id=%s", (subject_id,))
            max_of = {r['id']: float(r['max_marks']) for r in cur.fetchall()}
            cur.execute("SELECT id FROM students WHERE course=%s AND semester=%s",
                        (sub['course_code'], sub['semester']))
            valid_students = {r['id'] for r in cur.fetchall()}

            to_save, to_clear = [], []
            for e in entries:
                aid, sid = to_int(e.get('assessment_id')), to_int(e.get('student_id'))
                if aid not in max_of or sid not in valid_students:
                    continue
                raw = e.get('marks')
                if raw is None or str(raw).strip() == '':
                    to_clear.append((aid, sid))
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    return fail('Marks must be numbers.')
                if not (0 <= val <= max_of[aid]):
                    return fail(f'Marks must be between 0 and {max_of[aid]:g}.')
                to_save.append((aid, sid, round(val, 2)))

            if to_save:
                cur.executemany("""
                    INSERT INTO assessment_marks (assessment_id, student_id, marks) VALUES (%s,%s,%s)
                    ON DUPLICATE KEY UPDATE marks = VALUES(marks)
                """, to_save)
            if to_clear:
                cur.executemany("DELETE FROM assessment_marks WHERE assessment_id=%s AND student_id=%s",
                                to_clear)
        return jsonify({'success': True, 'message': 'Marks saved.'})
    except mysql.connector.Error as err:
        return db_error(err)


# =====================================================================
#  Student: my attendance, timetable and grades (read-only)
# =====================================================================
def current_student(cur):
    cur.execute("""SELECT id, registration_no, full_name, course, semester
                   FROM students WHERE registration_no = %s""", (session.get('id_number'),))
    return cur.fetchone()


def student_semester(stu):
    """Semester requested via ?semester=; defaults to current. None if out of range."""
    sem = to_int(request.args.get('semester'), stu['semester'])
    if sem is None or sem < 1 or sem > stu['semester']:
        return None
    return sem


@app.route('/api/student/attendance', methods=['GET'])
def student_attendance():
    if session.get('role') != 'student':
        return unauthorized()
    try:
        with db_cursor() as cur:
            stu = current_student(cur)
            if not stu:
                return fail('Student record not found.', 404)
            sem = student_semester(stu)
            if sem is None:
                return fail('Invalid semester.')

            cur.execute("""
                SELECT s.id, s.subject_code, s.subject_name, s.subject_type, s.credits,
                       f.full_name AS faculty_name,
                       COUNT(a.id) AS total,
                       COALESCE(SUM(a.status = 'Present'), 0) AS present
                FROM subjects s
                LEFT JOIN faculty_assignments fa ON fa.subject_id = s.id
                LEFT JOIN faculty f ON f.id = fa.faculty_id
                LEFT JOIN attendance a ON a.subject_id = s.id AND a.student_id = %s
                WHERE s.course_code = %s AND s.semester = %s
                GROUP BY s.id, f.full_name
                ORDER BY s.subject_code
            """, (stu['id'], stu['course'], sem))
            subjects = cur.fetchall()

            cur.execute("""
                SELECT a.att_date, a.status, s.subject_code, s.subject_name
                FROM attendance a JOIN subjects s ON s.id = a.subject_id
                WHERE a.student_id = %s AND s.course_code = %s AND s.semester = %s
                ORDER BY a.att_date DESC, s.subject_code
                LIMIT 50
            """, (stu['id'], stu['course'], sem))
            records = serialize_rows(cur.fetchall())

        total_all = present_all = 0
        for s in subjects:
            s['total'] = int(s['total'])
            s['present'] = int(s['present'])
            s['absent'] = s['total'] - s['present']
            s['percentage'] = round(s['present'] * 100 / s['total'], 1) if s['total'] else None
            total_all += s['total']
            present_all += s['present']

        overall = {
            'total': total_all,
            'present': present_all,
            'absent': total_all - present_all,
            'percentage': round(present_all * 100 / total_all, 1) if total_all else None,
        }
        return jsonify({'success': True, 'semester': sem, 'current_semester': stu['semester'],
                        'course': stu['course'], 'subjects': subjects,
                        'records': records, 'overall': overall})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/student/timetable', methods=['GET'])
def student_timetable():
    if session.get('role') != 'student':
        return unauthorized()
    try:
        with db_cursor() as cur:
            stu = current_student(cur)
            if not stu:
                return fail('Student record not found.', 404)
            sem = student_semester(stu)
            if sem is None:
                return fail('Invalid semester.')

            cur.execute("""SELECT id, slot_order, time_label FROM timetable_slots
                           WHERE course_code = %s AND semester = %s""", (stu['course'], sem))
            slots = cur.fetchall()
            slots.sort(key=lambda s: (time_sort_key(s['time_label']), s['slot_order'], s['id']))

            cur.execute("""
                SELECT e.slot_id, e.day_name, e.entry_type, e.custom_text,
                       s.subject_code, s.subject_name, s.subject_type,
                       f.full_name AS faculty_name
                FROM timetable_entries e
                JOIN timetable_slots t ON t.id = e.slot_id
                LEFT JOIN subjects s ON s.id = e.subject_id
                LEFT JOIN faculty_assignments fa ON fa.subject_id = s.id
                LEFT JOIN faculty f ON f.id = fa.faculty_id
                WHERE t.course_code = %s AND t.semester = %s
            """, (stu['course'], sem))
            entries = cur.fetchall()

        return jsonify({'success': True, 'semester': sem, 'current_semester': stu['semester'],
                        'course': stu['course'], 'days': DAYS,
                        'today': datetime.now().strftime('%A'),
                        'slots': slots, 'entries': entries})
    except mysql.connector.Error as err:
        return db_error(err)


@app.route('/api/student/grades', methods=['GET'])
def student_grades():
    if session.get('role') != 'student':
        return unauthorized()
    try:
        with db_cursor() as cur:
            stu = current_student(cur)
            if not stu:
                return fail('Student record not found.', 404)
            sem = student_semester(stu)
            if sem is None:
                return fail('Invalid semester.')

            cur.execute("""
                SELECT s.id, s.subject_code, s.subject_name, s.subject_type, s.credits,
                       f.full_name AS faculty_name
                FROM subjects s
                LEFT JOIN faculty_assignments fa ON fa.subject_id = s.id
                LEFT JOIN faculty f ON f.id = fa.faculty_id
                WHERE s.course_code = %s AND s.semester = %s
                ORDER BY s.subject_code
            """, (stu['course'], sem))
            subjects = cur.fetchall()

            cur.execute("""
                SELECT a.id, a.subject_id, a.title, a.category, a.max_marks, m.marks
                FROM assessments a
                JOIN subjects s ON s.id = a.subject_id
                LEFT JOIN assessment_marks m ON m.assessment_id = a.id AND m.student_id = %s
                WHERE s.course_code = %s AND s.semester = %s
                ORDER BY a.id
            """, (stu['id'], stu['course'], sem))
            assessments = cur.fetchall()

        by_id = {}
        for s in subjects:
            s['assessments'] = []
            s['obtained'] = 0.0
            s['max_entered'] = 0.0
            s['percentage'] = None
            by_id[s['id']] = s

        total_obt = total_max = 0.0
        for a in assessments:
            sub = by_id.get(a['subject_id'])
            if not sub:
                continue
            mx = float(a['max_marks'])
            mk = float(a['marks']) if a['marks'] is not None else None
            sub['assessments'].append({'id': a['id'], 'title': a['title'], 'category': a['category'],
                                       'max_marks': mx, 'marks': mk})
            if mk is not None:
                sub['obtained'] += mk
                sub['max_entered'] += mx
                total_obt += mk
                total_max += mx

        for s in subjects:
            s['obtained'] = round(s['obtained'], 2)
            s['max_entered'] = round(s['max_entered'], 2)
            if s['max_entered'] > 0:
                s['percentage'] = round(s['obtained'] * 100 / s['max_entered'], 1)

        overall = {'obtained': round(total_obt, 2), 'max': round(total_max, 2),
                   'percentage': round(total_obt * 100 / total_max, 1) if total_max > 0 else None}
        return jsonify({'success': True, 'semester': sem, 'current_semester': stu['semester'],
                        'course': stu['course'], 'subjects': subjects, 'overall': overall})
    except mysql.connector.Error as err:
        return db_error(err)


# =====================================================================
#  Admin dashboard stats
# =====================================================================
@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    """Real numbers for the admin dashboard."""
    if session.get('role') != 'admin':
        return unauthorized()
    try:
        with db_cursor() as cur:
            def count(sql):
                cur.execute(sql)
                return int(cur.fetchone()['n'])

            totals = {
                'students': count("SELECT COUNT(*) AS n FROM students"),
                'faculty':  count("SELECT COUNT(*) AS n FROM faculty"),
                'courses':  count("SELECT COUNT(*) AS n FROM courses"),
                'subjects': count("SELECT COUNT(*) AS n FROM subjects"),
                'unassigned_subjects': count("""
                    SELECT COUNT(*) AS n FROM subjects s
                    LEFT JOIN faculty_assignments fa ON fa.subject_id = s.id
                    WHERE fa.id IS NULL"""),
            }

            cur.execute("""
                SELECT c.course_code, c.course_name, c.total_semesters,
                       (SELECT COUNT(*) FROM students st WHERE st.course = c.course_code) AS students,
                       (SELECT COUNT(*) FROM subjects sb WHERE sb.course_code = c.course_code) AS subjects
                FROM courses c ORDER BY c.course_code
            """)
            courses = cur.fetchall()

            cur.execute("""
                SELECT registration_no, full_name, course_sem
                FROM students ORDER BY id DESC LIMIT 5
            """)
            recent_students = cur.fetchall()

            cur.execute("""
                SELECT employee_id, full_name, designation
                FROM faculty ORDER BY id DESC LIMIT 5
            """)
            recent_faculty = cur.fetchall()

        return jsonify({'success': True, 'totals': totals, 'courses': courses,
                        'recent_students': recent_students, 'recent_faculty': recent_faculty})
    except mysql.connector.Error as err:
        return db_error(err)


def open_browser():
    webbrowser.open_new(f'http://localhost:{PORT}/')


if __name__ == '__main__':
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        initialize_database()
        threading.Timer(1.2, open_browser).start()
    # host 0.0.0.0 lets the face system reach the portal even from another PC on the same network
    app.run(debug=True, host='0.0.0.0', port=PORT)