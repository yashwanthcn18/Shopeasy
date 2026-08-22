# models.py - All database tables are defined here

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ── User ──────────────────────────────────────────────────────────────────────
class User(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    first_name   = db.Column(db.String(100))
    last_name    = db.Column(db.String(100))
    name         = db.Column(db.String(100), nullable=False)   # kept for backward compat
    email        = db.Column(db.String(150), unique=True, nullable=False)
    password     = db.Column(db.String(200), nullable=False)
    phone        = db.Column(db.String(20))
    role         = db.Column(db.String(20), default='customer')   # customer / admin
    is_active    = db.Column(db.Boolean, default=True)
    is_verified  = db.Column(db.Boolean, default=False)
    verify_token = db.Column(db.String(100))

    @property
    def full_name(self):
        if self.first_name:
            return f"{self.first_name} {self.last_name or ''}".strip()
        return self.name

    def set_name(self, first, last=''):
        self.first_name = first.strip()
        self.last_name  = last.strip()
        self.name       = f"{first.strip()} {last.strip()}".strip()


# ── Product ───────────────────────────────────────────────────────────────────
class Product(db.Model):
    id                = db.Column(db.Integer, primary_key=True)
    name              = db.Column(db.String(200), nullable=False)
    description       = db.Column(db.Text)
    short_description = db.Column(db.String(500))
    price             = db.Column(db.Float, nullable=False)
    compare_price     = db.Column(db.Float)          # MRP / original price shown as strikethrough
    sku               = db.Column(db.String(100), unique=True)
    brand             = db.Column(db.String(100))
    category          = db.Column(db.String(100))
    image_url         = db.Column(db.String(300))
    stock             = db.Column(db.Integer, default=10)
    is_active         = db.Column(db.Boolean, default=True)

    @property
    def discount_percent(self):
        if self.compare_price and self.compare_price > self.price:
            return int((1 - self.price / self.compare_price) * 100)
        return 0


# ── Cart Item ─────────────────────────────────────────────────────────────────
class CartItem(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, default=1)

    product = db.relationship('Product')


# ── Order ─────────────────────────────────────────────────────────────────────
class Order(db.Model):
    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total              = db.Column(db.Float, nullable=False)
    address            = db.Column(db.Text, nullable=False)
    status             = db.Column(db.String(50), default='Confirmed')
    payment_status     = db.Column(db.String(50), default='Paid')
    fulfillment_status = db.Column(db.String(50), default='Unfulfilled')
    created_at         = db.Column(db.DateTime, server_default=db.func.now())

    items    = db.relationship('OrderItem', backref='order')
    payments = db.relationship('Payment', backref='order')


# ── Order Item ────────────────────────────────────────────────────────────────
class OrderItem(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    order_id     = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id   = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product_name = db.Column(db.String(200))    # snapshot at time of purchase
    quantity     = db.Column(db.Integer, nullable=False)
    price        = db.Column(db.Float, nullable=False)

    product = db.relationship('Product')


# ── Payment ───────────────────────────────────────────────────────────────────
class Payment(db.Model):
    id                     = db.Column(db.Integer, primary_key=True)
    order_id               = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    razorpay_order_id      = db.Column(db.String(100))
    razorpay_payment_id    = db.Column(db.String(100), unique=True)
    amount                 = db.Column(db.Float, nullable=False)
    currency               = db.Column(db.String(10), default='INR')
    status                 = db.Column(db.String(30), default='succeeded')
    failure_reason         = db.Column(db.String(255))
    created_at             = db.Column(db.DateTime, server_default=db.func.now())


# ── Saved Address ─────────────────────────────────────────────────────────────
class Address(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name       = db.Column(db.String(100))
    line1      = db.Column(db.String(200))
    line2      = db.Column(db.String(200))
    city       = db.Column(db.String(100))
    state      = db.Column(db.String(100))
    pincode    = db.Column(db.String(20))
    phone      = db.Column(db.String(20))
    is_default = db.Column(db.Boolean, default=False)


# ── Wishlist ──────────────────────────────────────────────────────────────────
class Wishlist(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product    = db.relationship('Product')


# ── Newsletter Subscriber ─────────────────────────────────────────────────────
class NewsletterSubscriber(db.Model):
    id    = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
