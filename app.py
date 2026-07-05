# app.py - Main Flask application. All routes live here.

from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from models import db, User, Product, CartItem, Order, OrderItem
import os

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')  # reads from environment on Render
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///shop.db'   # SQLite file created automatically
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'images')   # where uploaded images are saved
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}

db.init_app(app)


# ── Automatically pass cart_count to every template ───────────────────────────
# This is how the badge number shows on the cart icon across all pages
@app.context_processor
def inject_cart_count():
    if 'user_id' in session:
        count = CartItem.query.filter_by(user_id=session['user_id']).count()
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


# ── Helper: get the logged-in user (returns None if not logged in) ─────────────
def current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None


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

        # Hash the password before saving — never store plain text
        hashed = generate_password_hash(password)
        user   = User(name=name, email=email, password=hashed)
        db.session.add(user)
        db.session.commit()

        session['user_id'] = user.id
        return redirect(url_for('home'))

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

        session['user_id'] = user.id
        return redirect(url_for('home'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ═════════════════════════════════════════════════════════════════════════════
#  HOME — product listing
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    if not current_user():
        return redirect(url_for('login'))

    products = Product.query.all()
    return render_template('home.html', products=products, user=current_user())


# ═════════════════════════════════════════════════════════════════════════════
#  CART
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/cart/add/<int:product_id>')
def add_to_cart(product_id):
    if not current_user():
        return redirect(url_for('login'))

    user_id = session['user_id']

    # If the item is already in cart, just increase quantity
    item = CartItem.query.filter_by(user_id=user_id, product_id=product_id).first()
    if item:
        item.quantity += 1
    else:
        item = CartItem(user_id=user_id, product_id=product_id, quantity=1)
        db.session.add(item)

    db.session.commit()
    return redirect(url_for('home'))   # stay on home page after adding


# ── Update quantity (+ or -) ──────────────────────────────────────────────────
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
            # If quantity hits 0, remove the item entirely
            db.session.delete(item)
            db.session.commit()

    return redirect(url_for('cart'))


@app.route('/cart/remove/<int:item_id>')
def remove_from_cart(item_id):
    item = CartItem.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    return redirect(url_for('cart'))


@app.route('/cart')
def cart():
    if not current_user():
        return redirect(url_for('login'))

    items = CartItem.query.filter_by(user_id=session['user_id']).all()
    total = sum(i.product.price * i.quantity for i in items)
    return render_template('cart.html', items=items, total=total, user=current_user())


# ═════════════════════════════════════════════════════════════════════════════
#  CHECKOUT
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if not current_user():
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

if __name__ == '__main__':
    with app.app_context():
        db.create_all()      # creates shop.db and all tables if they don't exist
        seed_products()      # adds sample products on first run
    app.run(host='0.0.0.0', port=10000)
