# app.py - Main Flask application. All routes live here.

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from models import db, User, Product, CartItem, Order, OrderItem, NewsletterSubscriber
import os, re, random, time, requests, secrets

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')  # reads from environment on Render
app.permanent_session_lifetime = 60 * 60 * 24 * 30   # session lasts 30 days — only clears on manual logout
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///shop.db')   # uses Supabase on Render, SQLite locally
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'images')   # where uploaded images are saved
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}

db.init_app(app)


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
    if 'user_id' in session:
        count = CartItem.query.filter_by(user_id=session['user_id']).count()
        return {'cart_count': count}
    elif session.get('guest'):
        count = sum(session.get('guest_cart', {}).values())
        return {'cart_count': count}
    return {'cart_count': 0}


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
        name     = request.form['name']
        email    = request.form['email']
        password = request.form['password']

        # Check if email already exists
        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please log in.')
            return redirect(url_for('login'))

        hashed = generate_password_hash(password)
        token  = secrets.token_urlsafe(32)
        user   = User(name=name, email=email, password=hashed,
                      is_verified=False, verify_token=token)
        db.session.add(user)
        db.session.commit()

        # Send verification email — do NOT log in yet, require verification first
        link = url_for('verify_email', token=token, _external=True)
        sent = send_email(
            email,
            'ShopEasy — Please verify your email',
            f"Hi {name},\n\nClick the link below to verify your ShopEasy account:\n\n{link}\n\nThis link works only once.\n\n— ShopEasy Team"
        )

        if sent:
            flash('Account created! Check your email and click the verification link to login.')
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


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        user.name  = request.form['name']
        new_email  = request.form['email']
        if new_email != user.email and User.query.filter_by(email=new_email).first():
            flash('That email is already used by another account.')
            return redirect(url_for('profile'))
        user.email = new_email
        if request.form.get('password'):
            user.password = generate_password_hash(request.form['password'])
        db.session.commit()
        flash('Profile updated.')
        return redirect(url_for('profile'))
    user_orders = Order.query.filter_by(user_id=user.id).order_by(Order.id.desc()).all()
    return render_template('profile.html', user=user, orders=user_orders)


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
    if not current_user():
        return jsonify(items=[], total=0)

    if session.get('guest'):
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
    return render_template('home.html', products=products, user=current_user(), q=q, sort=sort)


# ═════════════════════════════════════════════════════════════════════════════
#  CART
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/cart/add/<int:product_id>')
def add_to_cart(product_id):
    if not current_user():
        return redirect(url_for('login'))

    if session.get('guest'):
        cart = session.get('guest_cart', {})
        key  = str(product_id)
        cart[key] = cart.get(key, 0) + 1
        session['guest_cart'] = cart
        return redirect(url_for('home'))

    user_id = session['user_id']
    item = CartItem.query.filter_by(user_id=user_id, product_id=product_id).first()
    if item:
        item.quantity += 1
    else:
        db.session.add(CartItem(user_id=user_id, product_id=product_id, quantity=1))
    db.session.commit()
    return redirect(url_for('home'))


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
    if not current_user():
        return redirect(url_for('login'))

    if session.get('guest'):
        guest_cart = session.get('guest_cart', {})
        items, total = [], 0
        for pid_str, qty in guest_cart.items():
            p = Product.query.get(int(pid_str))
            if p:
                items.append({'product': p, 'quantity': qty, 'id': p.id})
                total += p.price * qty
        return render_template('cart.html', items=items, total=total,
                               user=current_user(), is_guest=True)

    items = CartItem.query.filter_by(user_id=session['user_id']).all()
    total = sum(i.product.price * i.quantity for i in items)
    return render_template('cart.html', items=items, total=total, user=current_user(), is_guest=False)


# ═════════════════════════════════════════════════════════════════════════════
#  CHECKOUT
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if not current_user():
        return redirect(url_for('login'))
    if session.get('guest'):
        flash('Please log in or create an account to complete your order.')
        return redirect(url_for('login'))

    user_id = session['user_id']
    items   = CartItem.query.filter_by(user_id=user_id).all()

    if not items:
        flash('Your cart is empty.')
        return redirect(url_for('cart'))

    total = sum(i.product.price * i.quantity for i in items)

    if request.method == 'POST':
        address = request.form['address']

        # Create the order
        order = Order(user_id=user_id, total=total, address=address)
        db.session.add(order)
        db.session.flush()   # get order.id before committing

        # Copy each cart item into order items (snapshot of prices)
        for i in items:
            db.session.add(OrderItem(
                order_id   = order.id,
                product_id = i.product_id,
                quantity   = i.quantity,
                price      = i.product.price
            ))

        # Clear the cart
        CartItem.query.filter_by(user_id=user_id).delete()
        db.session.commit()

        flash('Order placed successfully!')
        return redirect(url_for('orders'))

    return render_template('checkout.html', items=items, total=total, user=current_user())


# ═════════════════════════════════════════════════════════════════════════════
#  MY ORDERS
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/orders')
def orders():
    if not current_user():
        return redirect(url_for('login'))

    user_orders = Order.query.filter_by(user_id=session['user_id']).order_by(Order.id.desc()).all()
    return render_template('orders.html', orders=user_orders, user=current_user())


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

        product = Product(name=name, description=description, price=price,
                          stock=stock, image_url=image_url)
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
        product.name        = request.form['name']
        product.description = request.form['description']
        product.price       = float(request.form['price'])
        product.stock       = int(request.form['stock'])

        # Only update image if a new one was uploaded or URL was changed
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
            pass  # column already exists — safe to ignore

    seed_products()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
