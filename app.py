from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os, json, base64
from openai import OpenAI

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'peakself-secret-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///peakself.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY', ''))

# ── Models ──────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(150), unique=True, nullable=False)
    username      = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    plan          = db.Column(db.String(20), default='free')   # free | monthly | quarterly
    credits       = db.Column(db.Integer, default=0)           # single-analysis credits
    sub_end       = db.Column(db.DateTime, nullable=True)
    analyses_left = db.Column(db.Integer, default=0)           # weekly analyses remaining
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    analyses      = db.relationship('Analysis', backref='user', lazy=True)

    def set_password(self, pw):  self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

    @property
    def is_subscribed(self):
        return self.sub_end and self.sub_end > datetime.utcnow()

    @property
    def can_analyze(self):
        return self.credits > 0 or (self.is_subscribed and self.analyses_left > 0)


class Analysis(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    score       = db.Column(db.Integer)
    report_json = db.Column(db.Text)   # JSON string

    @property
    def report(self):
        return json.loads(self.report_json) if self.report_json else {}


@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))


# ── Helpers ──────────────────────────────────────────────────────────────────

def build_prompt(form, has_photo):
    lifestyle = {
        "sleep":      form.get('sleep'),
        "sport":      form.get('sport'),
        "skincare":   form.get('skincare'),
        "diet":       form.get('diet'),
        "style":      form.get('style'),
        "haircut":    form.get('haircut'),
        "confidence": form.get('confidence'),
        "goal":       form.get('goal'),
        "hydration":  form.get('hydration'),
        "screentime": form.get('screentime'),
    }
    photo_note = "A selfie photo has been provided — analyze visible skin quality, hair, posture and overall grooming." if has_photo else "No photo provided — base your analysis only on lifestyle data."

    return f"""You are PeakSelf, a kind and science-based personal development coach helping young adults become the best version of themselves.

{photo_note}

Lifestyle data:
- Sleep: {lifestyle['sleep']} hours/night
- Exercise: {lifestyle['sport']} times/week
- Skincare routine: {lifestyle['skincare']}
- Diet quality: {lifestyle['diet']}
- Current style: {lifestyle['style']}
- Last haircut: {lifestyle['haircut']}
- Self-confidence level: {lifestyle['confidence']}/5
- Main goal: {lifestyle['goal']}
- Daily water intake: {lifestyle['hydration']}
- Daily screen time: {lifestyle['screentime']} hours

Generate a JSON response (and ONLY JSON, no markdown) with this exact structure:
{{
  "score": <integer 1-100 representing unlocked potential>,
  "score_label": "<short phrase describing the score, e.g. 'Solid Foundation'>",
  "summary": "<2-sentence warm, encouraging summary of their current situation>",
  "top3": [
    {{"area": "<area name>", "insight": "<what you noticed>", "action": "<concrete free action this week>"}},
    {{"area": "<area name>", "insight": "<what you noticed>", "action": "<concrete free action this week>"}},
    {{"area": "<area name>", "insight": "<what you noticed>", "action": "<concrete free action this week>"}}
  ],
  "skincare_tip": "<1 specific skincare advice for their profile>",
  "fitness_tip": "<1 specific fitness advice>",
  "style_tip": "<1 specific style or grooming advice>",
  "mindset_tip": "<1 motivational mindset advice>",
  "week1_plan": [
    "<Day 1-2: specific action>",
    "<Day 3-4: specific action>",
    "<Day 5-7: specific action>"
  ],
  "motivation": "<1 powerful, personal closing message addressing them directly>"
}}

Rules:
- NEVER mention surgery, extreme diets, or dangerous practices
- Be warm, non-judgmental, science-based
- All advice must be FREE and actionable today
- Score reflects healthy habits unlocked, NOT physical attractiveness
"""


def call_openai(prompt, image_b64=None):
    messages = [{"role": "user", "content": []}]
    if image_b64:
        messages[0]["content"].append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}", "detail": "low"}
        })
    messages[0]["content"].append({"type": "text", "text": prompt})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=1200,
        temperature=0.7,
    )
    raw = resp.choices[0].message.content.strip()
    # strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email    = request.form.get('email', '').lower().strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
        user = User(email=email, username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form.get('email', '').lower().strip()
        password = request.form.get('password', '')
        user     = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    analyses = Analysis.query.filter_by(user_id=current_user.id)\
                             .order_by(Analysis.created_at.desc()).all()
    return render_template('dashboard.html', analyses=analyses)


@app.route('/analyze', methods=['GET', 'POST'])
@login_required
def analyze():
    if request.method == 'GET':
        if not current_user.can_analyze:
            flash('You have no analyses available. Please purchase one below.', 'info')
            return redirect(url_for('pricing'))
        return render_template('analyze.html')

    # POST — run the analysis
    if not current_user.can_analyze:
        return jsonify({'error': 'No credits available'}), 403

    image_b64 = None
    if 'photo' in request.files:
        photo = request.files['photo']
        if photo and photo.filename:
            raw = photo.read(800_000)          # max 800 KB
            image_b64 = base64.b64encode(raw).decode('utf-8')

    prompt = build_prompt(request.form, image_b64 is not None)

    try:
        report = call_openai(prompt, image_b64)
    except Exception as e:
        return jsonify({'error': f'AI error: {str(e)}'}), 500

    # deduct credit
    if current_user.credits > 0:
        current_user.credits -= 1
    else:
        current_user.analyses_left -= 1

    analysis = Analysis(
        user_id     = current_user.id,
        score       = report.get('score', 50),
        report_json = json.dumps(report)
    )
    db.session.add(analysis)
    db.session.commit()
    return jsonify({'redirect': url_for('report', aid=analysis.id)})


@app.route('/report/<int:aid>')
@login_required
def report(aid):
    analysis = Analysis.query.get_or_404(aid)
    if analysis.user_id != current_user.id:
        return redirect(url_for('dashboard'))
    return render_template('report.html', analysis=analysis)


@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


# ── Stripe Webhooks (stub — replace with real Stripe keys) ───────────────────

@app.route('/buy/<plan>')
@login_required
def buy(plan):
    """
    In production: redirect to Stripe Checkout session.
    For now: simulate a successful purchase so you can test the full flow.
    """
    if plan == 'single':
        current_user.credits += 1
    elif plan == 'monthly':
        current_user.plan          = 'monthly'
        current_user.sub_end       = datetime.utcnow() + timedelta(days=30)
        current_user.analyses_left = 4
    elif plan == 'quarterly':
        current_user.plan          = 'quarterly'
        current_user.sub_end       = datetime.utcnow() + timedelta(days=90)
        current_user.analyses_left = 12
    db.session.commit()
    flash('Purchase successful! You can now start your analysis.', 'success')
    return redirect(url_for('analyze'))


# ── Init ─────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
