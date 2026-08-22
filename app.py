# app.py - Main Flask application. All routes live here.

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from models import db, User, Product, CartItem, Order, OrderItem, NewsletterSubscriber, Address, Payment, Wishlist
import os, re, random, time, requests, secrets, hmac, hashlib
import razorpay
from authlib.integrations.flask_client import OAuth

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')  # reads from environment on Render
app.permanent_session_lifetime = 60 * 60 * 24 * 30   # session lasts 30 days — only clears on manual logout
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///shop.db')   # uses Supabase on Render, SQLite locally
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'images')   # where uploaded images are saved
# Force https in url_for when behind Render's proxy
app.config['PREFERRED_URL_SCHEME'] = 'https'
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}

db.init_app(app)

# ── Razorpay client ───────────────────────────────────────────────────────────
def get_razorpay_client():
    return razorpay.Client(auth=(
        os.environ.get('RAZORPAY_KEY_ID', ''),
        os.environ.get('RAZORPAY_KEY_SECRET', '')
    ))

# ── OAuth (Google + Facebook social login) ────────────────────────────────────
oauth = OAuth(app)

google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

facebook = oauth.register(
    name='facebook',
    client_id=os.environ.get('FACEBOOK_APP_ID'),
    client_secret=os.environ.get('FACEBOOK_APP_SECRET'),
    access_token_url='https://graph.facebook.com/oauth/access_token',
    authorize_url='https://www.facebook.com/dialog/oauth',
    api_base_url='https://graph.facebook.com/',
    client_kwargs={'scope': 'email public_profile'},
)


# ── Email helper — Mailjet HTTP API (works on Render free tier) ───────────────
# Render blocks SMTP ports, so we use Mailjet's HTTP API instead
# Set MAILJET_API_KEY and MAILJET_SECRET_KEY in Render environment variables
def send_email(to_email, subject, body):
    api_key    = os.environ.get('MAILJET_API_KEY', '')
    secret_key = os.environ.get('MAILJET_SECRET_KEY', '')
    sender     = os.environ.get('MAIL_USERNAME', '')

    if not api_key or not secret_key:
        app.logger.warning('Mailjet credentials not set in environment')
        return False

    try:
        resp = requests.post(
            'https://api.mailjet.com/v3.1/send',
            auth=(api_key, secret_key),          # Mailjet uses HTTP Basic auth
            json={
                'Messages': [{
                    'From':     {'Email': sender, 'Name': 'ShopEasy'},
                    'To':       [{'Email': to_email}],
                    'Subject':  subject,
                    'TextPart': body,
                }]
            },
            timeout=10
        )
        if resp.status_code == 200:
            return True
        # Log full response so we can debug in Render logs
        app.logger.error(f'Mailjet error {resp.status_code}: {resp.text}')
        return False
    except Exception as e:
        app.logger.error(f'Email send failed: {e}')
        return False


# ── Automatically pass cart_count to every template ───────────────────────────
# This is how the badge number shows on the cart icon across all pages
@app.context_processor
def inject_cart_count():
    wishlist_count = 0
    if 'user_id' in session:
        count = CartItem.query.filter_by(user_id=session['user_id']).count()
        wishlist_count = Wishlist.query.filter_by(user_id=session['user_id']).count()
        return {'cart_count': count, 'wishlist_count': wishlist_count}
    elif session.get('guest_cart'):
        count = sum(session['guest_cart'].values())
        return {'cart_count': count, 'wishlist_count': 0}
    return {'cart_count': 0, 'wishlist_count': 0}


# ── Jinja filter: count how many orders a user has ────────────────────────────
@app.template_filter('count_orders')
def count_orders(user_id):
    return Order.query.filter_by(user_id=user_id).count()


# ── Helper: check if uploaded file is an allowed image type ───────────────────
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Seed some sample products on first run ────────────────────────────────────
def seed_products():
    if Product.query.first():
        return   # already seeded, skip

    samples = [
        # ── The Medal Holder — 4 variants ─────────────────────────────────────
        Product(
            name        = 'The Medal Holder Lite',
            description = 'Plastic casing, wall mount with 3-pin nail. Pack of 4 holders. Available in Black.',
            price       = 599,
            image_url   = '/static/images/medal-lite-3.jpg',
            stock       = 50,
        ),
        Product(
            name        = 'The Magnetic Medal Holder',
            description = 'Magnetic attachment for easy medal display. No nails needed. Pack of 4.',
            price       = 899,
            image_url   = 'https://placehold.co/400x300?text=Magnetic+Medal+Holder',
            stock       = 50,
        ),
        Product(
            name        = 'The Minimalist Medal Holder',
            description = 'Clean, minimal design. Slim profile, wall mount. Pack of 4. Available in Black.',
            price       = 749,
            image_url   = 'https://placehold.co/400x300?text=Minimalist+Medal+Holder',
            stock       = 50,
        ),
        Product(
            name        = 'The Fridge Magnetic Medal Holder',
            description = 'Sticks to any magnetic surface like a fridge. No wall drilling needed. Pack of 4.',
            price       = 699,
            image_url   = 'https://placehold.co/400x300?text=Fridge+Magnetic+Holder',
            stock       = 50,
        ),
    ]
    db.session.add_all(samples)
    db.session.commit()


# ── Lightweight object returned for guest sessions ────────────────────────────
class GuestUser:
    name     = 'Guest'
    email    = ''
    id       = None
    is_guest = True


# ── Helper: get the logged-in user (returns GuestUser for guests, None otherwise)
def current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    if session.get('guest'):
        return GuestUser()
    return None


# ── Merge guest session cart into the logged-in user's DB cart ─────────────────
def merge_guest_cart(user_id):
    guest_cart = session.pop('guest_cart', {})
    session.pop('guest', None)
    for pid_str, qty in guest_cart.items():
        product_id = int(pid_str)
        item = CartItem.query.filter_by(user_id=user_id, product_id=product_id).first()
        if item:
            item.quantity += qty
        else:
            db.session.add(CartItem(user_id=user_id, product_id=product_id, quantity=qty))
    db.session.commit()


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        last_name  = request.form.get('last_name', '').strip()
        name       = request.form.get('name', '').strip()   # fallback for old form
        email      = request.form['email']
        password   = request.form['password']

        # Support both old single-name and new first/last name forms
        if first_name:
            display_name = f"{first_name} {last_name}".strip()
        else:
            display_name = name
            first_name   = name.split()[0] if name else ''
            last_name    = ' '.join(name.split()[1:]) if len(name.split()) > 1 else ''

        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please log in.')
            return redirect(url_for('login'))

        hashed = generate_password_hash(password)
        token  = secrets.token_urlsafe(32)
        user   = User(name=display_name, first_name=first_name, last_name=last_name,
                      email=email, password=hashed, is_verified=False, verify_token=token)
        db.session.add(user)
        db.session.commit()

        link = url_for('verify_email', token=token, _external=True)
        sent = send_email(
            email,
            'KAPIQ — Please verify your email',
            f"Hi {display_name},\n\nClick the link below to verify your KAPIQ account:\n\n{link}\n\nThis link works only once.\n\n— KAPIQ Team"
        )

        if sent:
            flash('Account created! Check your inbox (and spam/junk folder) for the verification link.')
        else:
            flash('Account created but we could not send a verification email. Contact support.')
        return redirect(url_for('login'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form['email']
        password = request.form['password']

        user = User.query.filter_by(email=email).first()

        # check_password_hash compares plain text with the stored hash
        if not user or not check_password_hash(user.password, password):
            flash('Invalid email or password.')
            return redirect(url_for('login'))

        # Block login if email is not verified yet
        if not user.is_verified:
            flash('Please verify your email first. Check your inbox for the verification link.')
            return redirect(url_for('login'))

        session.permanent = True
        session['user_id'] = user.id
        merge_guest_cart(user.id)
        flash(f'Welcome back, {user.full_name}!', 'login_success')
        next_page = request.args.get('next') or request.form.get('next')
        if next_page == 'checkout':
            return redirect(url_for('checkout'))
        return redirect(url_for('home'))

    return render_template('login.html')


@app.route('/resend-verification', methods=['POST'])
def resend_verification():
    email = request.form.get('email', '').strip()
    user  = User.query.filter_by(email=email).first()
    if user and not user.is_verified:
        token = secrets.token_urlsafe(32)
        user.verify_token = token
        db.session.commit()
        link = url_for('verify_email', token=token, _external=True)
        send_email(
            email,
            'ShopEasy — Verify your email',
            f"Hi {user.name},\n\nHere is your new verification link:\n\n{link}\n\n— ShopEasy Team"
        )
    # Always show same message to avoid email enumeration
    flash('If that email exists and is unverified, a new link has been sent.')
    return redirect(url_for('login'))


# ── Google OAuth ─────────────────────────────────────────────────────────────
@app.route('/auth/google')
def google_login():
    # Use env var so it works on both Render and locally
    redirect_uri = os.environ.get(
        'GOOGLE_REDIRECT_URI',
        url_for('google_callback', _external=True, _scheme='https')
    )
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/google/callback')
def google_callback():
    try:
        token     = google.authorize_access_token()
        user_info = token.get('userinfo')
        email     = user_info['email']
        name      = user_info.get('name', email.split('@')[0])
    except Exception as e:
        app.logger.error(f'Google OAuth error: {e}')
        flash('Google login failed. Please try again.')
        return redirect(url_for('login'))

    # Find existing user or create one (already verified via Google)
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(name=name, email=email,
                    password=generate_password_hash(secrets.token_hex(16)),
                    is_verified=True, verify_token=None)
        db.session.add(user)
        db.session.commit()
    elif not user.is_verified:
        # Mark existing unverified account as verified since Google confirmed the email
        user.is_verified = True
        db.session.commit()

    session.permanent  = True
    session['user_id'] = user.id
    merge_guest_cart(user.id)
    return redirect(url_for('home'))


# ── Facebook OAuth ────────────────────────────────────────────────────────────
@app.route('/auth/facebook')
def facebook_login():
    redirect_uri = url_for('facebook_callback', _external=True)
    return facebook.authorize_redirect(redirect_uri)

@app.route('/auth/facebook/callback')
def facebook_callback():
    try:
        facebook.authorize_access_token()
        resp      = facebook.get('me?fields=id,name,email')
        user_info = resp.json()
        email     = user_info.get('email')
        name      = user_info.get('name', 'User')
    except Exception as e:
        app.logger.error(f'Facebook OAuth error: {e}')
        flash('Facebook login failed. Please try again.')
        return redirect(url_for('login'))

    if not email:
        flash('Facebook did not share your email. Please use email/password login.')
        return redirect(url_for('login'))

    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(name=name, email=email,
                    password=generate_password_hash(secrets.token_hex(16)),
                    is_verified=True, verify_token=None)
        db.session.add(user)
        db.session.commit()
    elif not user.is_verified:
        user.is_verified = True
        db.session.commit()

    session.permanent  = True
    session['user_id'] = user.id
    merge_guest_cart(user.id)
    return redirect(url_for('home'))


@app.route('/verify-email/<token>')
def verify_email(token):
    user = User.query.filter_by(verify_token=token).first()
    if not user:
        flash('Invalid or already used verification link.')
        return redirect(url_for('login'))
    user.is_verified  = True
    user.verify_token = None
    db.session.commit()
    flash('Email verified! You are now fully verified.')
    return redirect(url_for('home'))


@app.route('/guest-login')
def guest_login():
    session['guest'] = True
    if 'guest_cart' not in session:
        session['guest_cart'] = {}
    return redirect(url_for('home'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Step 1: Enter email → receive OTP ────────────────────────────────────────
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email'].strip()
        user  = User.query.filter_by(email=email).first()
        if not user:
            flash('No account found with that email.')
            return redirect(url_for('forgot_password'))

        otp = str(random.randint(100000, 999999))
        session['otp']        = otp
        session['otp_email']  = email
        session['otp_expiry'] = time.time() + 300   # 5-minute expiry

        sent = send_email(
            email,
            'ShopEasy — Your password reset OTP',
            f"Hello,\n\nYour OTP for resetting your ShopEasy password is:\n\n  {otp}\n\nThis code is valid for 5 minutes. Do not share it with anyone.\n\n— ShopEasy Team"
        )
        if not sent:
            flash('Could not send email. Please check MAIL credentials in Render.')
            return redirect(url_for('forgot_password'))
        flash(f'OTP sent to {email}. Check your inbox and spam folder.')

        return redirect(url_for('verify_otp'))

    return render_template('forgot_password.html')


# ── Step 2: Enter OTP ─────────────────────────────────────────────────────────
@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    if 'otp' not in session:
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        entered = request.form['otp'].strip()

        if time.time() > session.get('otp_expiry', 0):
            session.pop('otp', None)
            flash('OTP has expired. Please request a new one.')
            return redirect(url_for('forgot_password'))

        if entered != session['otp']:
            flash('Incorrect OTP. Please try again.')
            return redirect(url_for('verify_otp'))

        session['otp_verified'] = True
        session.pop('otp', None)
        return redirect(url_for('reset_password'))

    return render_template('verify_otp.html')


# ── Step 3: Set new password ──────────────────────────────────────────────────
@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if not session.get('otp_verified'):
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        email        = session.get('otp_email')
        new_password = request.form['new_password']
        user = User.query.filter_by(email=email).first()
        if user:
            user.password = generate_password_hash(new_password)
            db.session.commit()
        session.pop('otp_verified', None)
        session.pop('otp_email', None)
        flash('Password updated successfully. Please log in.')
        return redirect(url_for('login'))

    return render_template('reset_password.html')


@app.route('/profile')
def profile_redirect():
    return redirect('/account')

@app.route('/account', methods=['GET', 'POST'])
def profile():
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_details':
            first = request.form.get('first_name', '').strip()
            last  = request.form.get('last_name', '').strip()
            if first:
                user.first_name = first
                user.last_name  = last
                user.name       = f"{first} {last}".strip()
            user.phone = request.form.get('phone', '').strip()
            new_pw = request.form.get('password', '').strip()
            if new_pw:
                user.password = generate_password_hash(new_pw)
            db.session.commit()
            flash('Account details updated.')

        elif action == 'change_email':
            new_email = request.form.get('email', '').strip().lower()
            if not new_email:
                flash('Email cannot be empty.')
            elif new_email == user.email:
                flash('That is already your current email.')
            elif User.query.filter_by(email=new_email).first():
                flash('That email is already used by another account.')
            else:
                token = secrets.token_urlsafe(32)
                user.verify_token = token
                user.is_verified  = False
                user.email        = new_email
                db.session.commit()
                link = url_for('verify_email', token=token, _external=True, _scheme='https')
                send_email(new_email, 'ShopEasy — Verify your new email',
                           f'Hi {user.name},\n\nPlease verify your new email:\n{link}\n\nThis link works once.')
                session.clear()
                flash('Email changed. A verification link was sent to your new email. Please verify before logging in again.')
                return redirect(url_for('login'))

        return redirect(url_for('profile'))

    user_orders = Order.query.filter_by(user_id=user.id).order_by(Order.id.desc()).all()
    addresses   = Address.query.filter_by(user_id=user.id).order_by(Address.is_default.desc()).all()
    return render_template('profile.html', user=user, orders=user_orders, addresses=addresses)


@app.route('/account/order/<int:order_id>')
def order_detail(order_id):
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    order = Order.query.filter_by(id=order_id, user_id=user.id).first_or_404()
    return render_template('order_detail.html', user=user, order=order)


@app.route('/account/addresses/add', methods=['POST'])
def add_address():
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    make_default = request.form.get('is_default') == '1'
    if make_default:
        Address.query.filter_by(user_id=user.id).update({'is_default': False})
    addr = Address(
        user_id    = user.id,
        name       = request.form.get('name', '').strip(),
        line1      = request.form.get('line1', '').strip(),
        line2      = request.form.get('line2', '').strip(),
        city       = request.form.get('city', '').strip(),
        state      = request.form.get('state', '').strip(),
        pincode    = request.form.get('pincode', '').strip(),
        phone      = request.form.get('phone', '').strip(),
        is_default = make_default,
    )
    db.session.add(addr)
    db.session.commit()
    flash('Address saved.')
    return redirect(url_for('profile') + '#addresses')


@app.route('/account/addresses/delete/<int:addr_id>', methods=['POST'])
def delete_address(addr_id):
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    addr = Address.query.filter_by(id=addr_id, user_id=user.id).first_or_404()
    db.session.delete(addr)
    db.session.commit()
    flash('Address removed.')
    return redirect(url_for('profile') + '#addresses')


@app.route('/account/addresses/default/<int:addr_id>', methods=['POST'])
def set_default_address(addr_id):
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    Address.query.filter_by(user_id=user.id).update({'is_default': False})
    addr = Address.query.filter_by(id=addr_id, user_id=user.id).first_or_404()
    addr.is_default = True
    db.session.commit()
    return redirect(url_for('profile') + '#addresses')


@app.route('/newsletter/subscribe', methods=['POST'])
def newsletter_subscribe():
    email = request.form.get('email', '').strip()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify(success=False, message='Invalid email address.')
    existing = NewsletterSubscriber.query.filter_by(email=email).first()
    if existing:
        return jsonify(success=True, message='You are already subscribed!')
    db.session.add(NewsletterSubscriber(email=email))
    db.session.commit()
    return jsonify(success=True, message='Thanks for subscribing!')


@app.route('/cart/data')
def cart_data():
    if 'user_id' not in session:
        # Guest cart from session
        result, total = [], 0
        for pid_str, qty in session.get('guest_cart', {}).items():
            p = Product.query.get(int(pid_str))
            if p:
                result.append({'id': p.id, 'name': p.name, 'price': p.price,
                                'quantity': qty, 'subtotal': p.price * qty, 'image': p.image_url})
                total += p.price * qty
        return jsonify(items=result, total=total)

    items = CartItem.query.filter_by(user_id=session['user_id']).all()
    total = sum(i.product.price * i.quantity for i in items)
    return jsonify(
        items=[{
            'id':       i.id,
            'name':     i.product.name,
            'price':    i.product.price,
            'quantity': i.quantity,
            'subtotal': i.product.price * i.quantity,
            'image':    i.product.image_url,
        } for i in items],
        total=total
    )


@app.route('/about')
def about():
    return render_template('pages/about.html', user=current_user())

@app.route('/privacy')
def privacy():
    return render_template('pages/privacy.html', user=current_user())

@app.route('/refund')
def refund():
    return render_template('pages/refund.html', user=current_user())

@app.route('/terms')
def terms():
    return render_template('pages/terms.html', user=current_user())

@app.route('/contact')
def contact():
    return render_template('pages/contact.html', user=current_user())


# ═════════════════════════════════════════════════════════════════════════════
#  HOME — product listing
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    q    = request.args.get('q', '').strip()
    sort = request.args.get('sort', '')

    query = Product.query
    if q:
        query = query.filter(
            Product.name.ilike(f'%{q}%') | Product.description.ilike(f'%{q}%')
        )
    if sort == 'low':
        query = query.order_by(Product.price.asc())
    elif sort == 'high':
        query = query.order_by(Product.price.desc())

    products = query.all()
    wishlist_ids = []
    if 'user_id' in session:
        wishlist_ids = [w.product_id for w in Wishlist.query.filter_by(user_id=session['user_id']).all()]
    return render_template('home.html', products=products, user=current_user(), q=q, sort=sort, wishlist_ids=wishlist_ids)


# ═════════════════════════════════════════════════════════════════════════════
#  CART
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/cart/add/<int:product_id>')
def add_to_cart(product_id):
    if 'user_id' in session:
        user_id = session['user_id']
        item = CartItem.query.filter_by(user_id=user_id, product_id=product_id).first()
        if item:
            item.quantity += 1
        else:
            db.session.add(CartItem(user_id=user_id, product_id=product_id, quantity=1))
        db.session.commit()
    else:
        # Guest — store in session
        cart = session.get('guest_cart', {})
        key  = str(product_id)
        cart[key] = cart.get(key, 0) + 1
        session['guest_cart'] = cart
        session.modified = True

    next_url = request.referrer or url_for('home')
    return redirect(next_url)


# ── Update quantity for logged-in users ───────────────────────────────────────
@app.route('/cart/update/<int:item_id>/<action>')
def update_cart(item_id, action):
    if not current_user():
        return redirect(url_for('login'))

    item = CartItem.query.get_or_404(item_id)
    if action == 'increase':
        item.quantity += 1
        db.session.commit()
    elif action == 'decrease':
        if item.quantity > 1:
            item.quantity -= 1
            db.session.commit()
        else:
            db.session.delete(item)
            db.session.commit()
    return redirect(url_for('cart'))


# ── Update quantity for guests ────────────────────────────────────────────────
@app.route('/cart/update-guest/<int:product_id>/<action>')
def update_guest_cart(product_id, action):
    cart = session.get('guest_cart', {})
    key  = str(product_id)
    if action == 'increase':
        cart[key] = cart.get(key, 0) + 1
    elif action == 'decrease':
        if cart.get(key, 1) > 1:
            cart[key] -= 1
        else:
            cart.pop(key, None)
    session['guest_cart'] = cart
    return redirect(url_for('cart'))


@app.route('/cart/remove/<int:item_id>')
def remove_from_cart(item_id):
    item = CartItem.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    return redirect(url_for('cart'))


@app.route('/cart/remove-guest/<int:product_id>')
def remove_guest_cart(product_id):
    cart = session.get('guest_cart', {})
    cart.pop(str(product_id), None)
    session['guest_cart'] = cart
    return redirect(url_for('cart'))


@app.route('/cart')
def cart():
    if 'user_id' in session:
        items = CartItem.query.filter_by(user_id=session['user_id']).all()
        total = sum(i.product.price * i.quantity for i in items)
        return render_template('cart.html', items=items, total=total, user=current_user(), is_guest=False)

    # Guest — read from session
    guest_cart = session.get('guest_cart', {})
    items, total = [], 0
    for pid_str, qty in guest_cart.items():
        p = Product.query.get(int(pid_str))
        if p:
            items.append({'product': p, 'quantity': qty, 'id': p.id})
            total += p.price * qty
    return render_template('cart.html', items=items, total=total, user=None, is_guest=True)


# ═════════════════════════════════════════════════════════════════════════════
#  WISHLIST
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/wishlist')
def wishlist():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    items = Wishlist.query.filter_by(user_id=session['user_id']).all()
    return render_template('wishlist.html', items=items, user=current_user())


@app.route('/wishlist/add/<int:product_id>')
def wishlist_add(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    if not Wishlist.query.filter_by(user_id=user_id, product_id=product_id).first():
        db.session.add(Wishlist(user_id=user_id, product_id=product_id))
        db.session.commit()
    return redirect(request.referrer or url_for('home'))


@app.route('/wishlist/remove/<int:product_id>')
def wishlist_remove(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    item = Wishlist.query.filter_by(user_id=session['user_id'], product_id=product_id).first()
    if item:
        db.session.delete(item)
        db.session.commit()
    return redirect(request.referrer or url_for('wishlist'))


# ═════════════════════════════════════════════════════════════════════════════
#  CHECKOUT
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/checkout', methods=['GET'])
def checkout():
    if 'user_id' not in session:
        flash('Please log in or create an account to complete your order.')
        return redirect(url_for('login', next='checkout'))

    user_id = session['user_id']
    items   = CartItem.query.filter_by(user_id=user_id).all()
    if not items:
        flash('Your cart is empty.')
        return redirect(url_for('cart'))

    user      = current_user()
    total     = sum(i.product.price * i.quantity for i in items)
    addresses = Address.query.filter_by(user_id=user_id).order_by(Address.is_default.desc()).all()
    razorpay_key = os.environ.get('RAZORPAY_KEY_ID', '')
    return render_template('checkout.html', items=items, total=total,
                           user=user, addresses=addresses, razorpay_key=razorpay_key)


@app.route('/checkout/create-order', methods=['POST'])
def create_razorpay_order():
    if not current_user():
        return jsonify(error='Not logged in'), 401

    user_id = session['user_id']
    items   = CartItem.query.filter_by(user_id=user_id).all()
    if not items:
        return jsonify(error='Cart is empty'), 400

    total = sum(i.product.price * i.quantity for i in items)
    amount_paise = int(total * 100)  # Razorpay uses paise (1 INR = 100 paise)

    try:
        client = get_razorpay_client()
        rz_order = client.order.create({
            'amount':   amount_paise,
            'currency': 'INR',
            'receipt':  f'order_user_{user_id}',
            'payment_capture': 1,
        })
        session['pending_address'] = request.json.get('address', '')
        return jsonify(
            razorpay_order_id = rz_order['id'],
            amount            = amount_paise,
            currency          = 'INR',
        )
    except Exception as e:
        app.logger.error(f'Razorpay order creation failed: {e}')
        return jsonify(error='Payment gateway error. Please try again.'), 500


@app.route('/checkout/verify-payment', methods=['POST'])
def verify_payment():
    if not current_user():
        return jsonify(error='Not logged in'), 401

    data               = request.json
    rz_order_id        = data.get('razorpay_order_id', '')
    rz_payment_id      = data.get('razorpay_payment_id', '')
    rz_signature       = data.get('razorpay_signature', '')
    address            = data.get('address', session.get('pending_address', ''))

    # Verify HMAC-SHA256 signature
    key_secret = os.environ.get('RAZORPAY_KEY_SECRET', '').encode()
    msg        = f'{rz_order_id}|{rz_payment_id}'.encode()
    expected   = hmac.new(key_secret, msg, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, rz_signature):
        return jsonify(error='Payment verification failed.'), 400

    user_id = session['user_id']
    items   = CartItem.query.filter_by(user_id=user_id).all()
    if not items:
        return jsonify(error='Cart is empty'), 400

    total = sum(i.product.price * i.quantity for i in items)

    order = Order(
        user_id            = user_id,
        total              = total,
        address            = address,
        payment_status     = 'Paid',
        fulfillment_status = 'Unfulfilled',
        status             = 'Confirmed',
    )
    db.session.add(order)
    db.session.flush()

    for i in items:
        db.session.add(OrderItem(
            order_id     = order.id,
            product_id   = i.product_id,
            product_name = i.product.name,
            quantity     = i.quantity,
            price        = i.product.price,
        ))

    db.session.add(Payment(
        order_id            = order.id,
        razorpay_order_id   = rz_order_id,
        razorpay_payment_id = rz_payment_id,
        amount              = total,
        currency            = 'INR',
        status              = 'succeeded',
    ))

    CartItem.query.filter_by(user_id=user_id).delete()
    session.pop('pending_address', None)
    db.session.commit()

    return jsonify(success=True, order_id=order.id)


# ═════════════════════════════════════════════════════════════════════════════
#  MY ORDERS
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/orders')
def orders():
    return redirect(url_for('profile') + '#orders')


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN ROUTES
#  All routes start with /admin — only accessible if session['is_admin'] is set
# ═════════════════════════════════════════════════════════════════════════════

# ── Helper: block non-admins ──────────────────────────────────────────────────
def admin_required():
    return session.get('is_admin') is True


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        # Read credentials from file if it exists, otherwise use defaults
        if os.path.exists('admin_credentials.txt'):
            with open('admin_credentials.txt') as f:
                lines        = f.read().splitlines()
                saved_user   = lines[0]
                saved_pass   = lines[1]
        else:
            saved_user = 'admin'
            saved_pass = 'admin@yash@12345'

        if request.form['username'] == saved_user and request.form['password'] == saved_pass:
            session.permanent = True
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Wrong username or password.')
    return render_template('admin/login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


# ── Change admin password — logs out immediately after saving ─────────────────
@app.route('/admin/change-password', methods=['GET', 'POST'])
def admin_change_password():
    if not admin_required():
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        new_username = request.form['username']
        new_password = request.form['password']

        # Write new credentials to a file so they persist across restarts
        with open('admin_credentials.txt', 'w') as f:
            f.write(f'{new_username}\n{new_password}')

        # Log out immediately so new password takes effect
        session.pop('is_admin', None)
        flash('Password changed. Please log in with your new credentials.')
        return redirect(url_for('admin_login'))

    return render_template('admin/change_password.html')


# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route('/admin')
def admin_dashboard():
    if not admin_required():
        return redirect(url_for('admin_login'))

    stats = {
        'products' : Product.query.count(),
        'orders'   : Order.query.count(),
        'users'    : User.query.count(),
        'revenue'  : db.session.query(db.func.sum(Order.total)).scalar() or 0,
    }
    recent_orders = Order.query.order_by(Order.id.desc()).limit(5).all()
    return render_template('admin/dashboard.html', stats=stats, recent_orders=recent_orders)


# ── Products list ─────────────────────────────────────────────────────────────
@app.route('/admin/products')
def admin_products():
    if not admin_required():
        return redirect(url_for('admin_login'))
    products = Product.query.all()
    return render_template('admin/products.html', products=products)


# ── Add product ───────────────────────────────────────────────────────────────
@app.route('/admin/products/add', methods=['GET', 'POST'])
def admin_add_product():
    if not admin_required():
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        name        = request.form['name']
        description = request.form['description']
        price       = float(request.form['price'])
        stock       = int(request.form['stock'])

        # Handle image upload if a file was provided
        image_url = request.form.get('image_url', '')
        file = request.files.get('image_file')
        if file and allowed_file(file.filename):
            filename  = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            image_url = f'/static/images/{filename}'

        compare_price     = request.form.get('compare_price')
        product = Product(
            name              = name,
            description       = description,
            short_description = request.form.get('short_description', ''),
            price             = price,
            compare_price     = float(compare_price) if compare_price else None,
            sku               = request.form.get('sku', '').strip() or None,
            brand             = request.form.get('brand', '').strip(),
            category          = request.form.get('category', '').strip(),
            stock             = stock,
            image_url         = image_url,
        )
        db.session.add(product)
        db.session.commit()
        flash('Product added.')
        return redirect(url_for('admin_products'))

    return render_template('admin/product_form.html', product=None)


# ── Edit product ──────────────────────────────────────────────────────────────
@app.route('/admin/products/edit/<int:product_id>', methods=['GET', 'POST'])
def admin_edit_product(product_id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    product = Product.query.get_or_404(product_id)

    if request.method == 'POST':
        product.name              = request.form['name']
        product.description       = request.form['description']
        product.short_description = request.form.get('short_description', '')
        product.price             = float(request.form['price'])
        product.stock             = int(request.form['stock'])
        compare_price             = request.form.get('compare_price')
        product.compare_price     = float(compare_price) if compare_price else None
        product.sku               = request.form.get('sku', '').strip() or None
        product.brand             = request.form.get('brand', '').strip()
        product.category          = request.form.get('category', '').strip()

        file = request.files.get('image_file')
        if file and allowed_file(file.filename):
            filename          = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            product.image_url = f'/static/images/{filename}'
        elif request.form.get('image_url'):
            product.image_url = request.form['image_url']

        db.session.commit()
        flash('Product updated.')
        return redirect(url_for('admin_products'))

    return render_template('admin/product_form.html', product=product)


# ── Delete product ────────────────────────────────────────────────────────────
@app.route('/admin/products/delete/<int:product_id>')
def admin_delete_product(product_id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    product = Product.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    flash('Product deleted.')
    return redirect(url_for('admin_products'))


# ── Orders list ───────────────────────────────────────────────────────────────
@app.route('/admin/orders')
def admin_orders():
    if not admin_required():
        return redirect(url_for('admin_login'))
    all_orders = Order.query.order_by(Order.id.desc()).all()
    return render_template('admin/orders.html', orders=all_orders)


# ── Update order status ───────────────────────────────────────────────────────
@app.route('/admin/orders/status/<int:order_id>', methods=['POST'])
def admin_update_status(order_id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    order        = Order.query.get_or_404(order_id)
    order.status = request.form['status']
    db.session.commit()
    flash(f'Order #{order_id} updated to {order.status}.')
    return redirect(url_for('admin_orders'))


# ── Users list ────────────────────────────────────────────────────────────────
@app.route('/admin/users')
def admin_users():
    if not admin_required():
        return redirect(url_for('admin_login'))
    users = User.query.all()
    return render_template('admin/users.html', users=users)


# ── Delete user (also removes their cart and orders) ──────────────────────────
@app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    user = User.query.get_or_404(user_id)

    # Delete related cart items and orders first to avoid foreign key errors
    CartItem.query.filter_by(user_id=user_id).delete()
    for order in Order.query.filter_by(user_id=user_id).all():
        OrderItem.query.filter_by(order_id=order.id).delete()
    Order.query.filter_by(user_id=user_id).delete()

    db.session.delete(user)
    db.session.commit()
    flash(f'User {user.name} deleted.')
    return redirect(url_for('admin_users'))


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

# ── Create tables on startup — works on both Render and locally ───────────────
# Also runs a safe migration to add any missing columns to existing tables
with app.app_context():
    db.create_all()

    # Migration: add is_verified and verify_token columns if they don't exist yet
    # (db.create_all won't add new columns to existing tables)
    with db.engine.connect() as conn:
        try:
            conn.execute(db.text('ALTER TABLE "user" ADD COLUMN is_verified BOOLEAN DEFAULT FALSE'))
            conn.commit()
            app.logger.info('Migration: added is_verified column')
        except Exception:
            pass  # column already exists — safe to ignore
        try:
            conn.execute(db.text('ALTER TABLE "user" ADD COLUMN verify_token VARCHAR(100)'))
            conn.commit()
            app.logger.info('Migration: added verify_token column')
        except Exception:
            pass
        # user columns
        for col in [
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS phone VARCHAR(20)',
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS first_name VARCHAR(100)',
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS last_name VARCHAR(100)',
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT \'customer\'',
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE',
        ]:
            try: conn.execute(db.text(col)); conn.commit()
            except Exception: pass

        # product columns
        for col in [
            'ALTER TABLE product ADD COLUMN IF NOT EXISTS short_description VARCHAR(500)',
            'ALTER TABLE product ADD COLUMN IF NOT EXISTS compare_price FLOAT',
            'ALTER TABLE product ADD COLUMN IF NOT EXISTS sku VARCHAR(100)',
            'ALTER TABLE product ADD COLUMN IF NOT EXISTS brand VARCHAR(100)',
            'ALTER TABLE product ADD COLUMN IF NOT EXISTS category VARCHAR(100)',
            'ALTER TABLE product ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE',
        ]:
            try: conn.execute(db.text(col)); conn.commit()
            except Exception: pass

        # order columns
        for col in [
            'ALTER TABLE "order" ADD COLUMN IF NOT EXISTS payment_status VARCHAR(50) DEFAULT \'Paid\'',
            'ALTER TABLE "order" ADD COLUMN IF NOT EXISTS fulfillment_status VARCHAR(50) DEFAULT \'Unfulfilled\'',
        ]:
            try: conn.execute(db.text(col)); conn.commit()
            except Exception: pass

        # order_item snapshot column
        try:
            conn.execute(db.text('ALTER TABLE order_item ADD COLUMN IF NOT EXISTS product_name VARCHAR(200)'))
            conn.commit()
        except Exception: pass

        # wishlist table — db.create_all() handles new DBs; this covers existing ones
        try:
            conn.execute(db.text('''
                CREATE TABLE IF NOT EXISTS wishlist (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES "user"(id),
                    product_id INTEGER NOT NULL REFERENCES product(id)
                )
            '''))
            conn.commit()
        except Exception:
            pass

    # Mark all existing users (who have no verify_token) as verified
    # so they aren't locked out after the verification feature was added
    User.query.filter_by(verify_token=None, is_verified=False).update({'is_verified': True})
    db.session.commit()

    seed_products()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
