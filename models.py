# models.py - All database tables are defined here

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ── User ──────────────────────────────────────────────────────────────────────
class User(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    email    = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)  # stored as hashed value


# ── Product ───────────────────────────────────────────────────────────────────
class Product(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    price       = db.Column(db.Float, nullable=False)
    image_url   = db.Column(db.String(300))   # URL or path to product image
    stock       = db.Column(db.Integer, default=10)


# ── Cart Item ─────────────────────────────────────────────────────────────────
# Each row = one product in one user's cart
class CartItem(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, default=1)

    product = db.relationship('Product')   # lets us do cart_item.product.name etc.


# ── Order ─────────────────────────────────────────────────────────────────────
class Order(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total      = db.Column(db.Float, nullable=False)
    address    = db.Column(db.Text, nullable=False)
    status     = db.Column(db.String(50), default='Placed')   # Placed / Shipped / Delivered
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    items = db.relationship('OrderItem', backref='order')


# ── Order Item ────────────────────────────────────────────────────────────────
# Snapshot of what was in the cart when the order was placed
class OrderItem(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    order_id   = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, nullable=False)
    price      = db.Column(db.Float, nullable=False)   # price at time of purchase

    product = db.relationship('Product')
