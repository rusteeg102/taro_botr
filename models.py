from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os
import pytz

Base = declarative_base()

# Абсолютный путь к БД — всегда рядом с этим файлом, независимо от cwd
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taro_bot.db')
engine = create_engine(f'sqlite:///{_DB_PATH}')
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    balance = Column(Float, default=0.0)
    has_used_free_reading = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.UTC))
    
    readings = relationship('Reading', back_populates='user')
    transactions = relationship('Transaction', back_populates='user')
    topup_requests = relationship('TopupRequest', back_populates='user')

class Reading(Base):
    __tablename__ = 'readings'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    question1 = Column(Text, nullable=False)
    question2 = Column(Text, nullable=False)
    question3 = Column(Text, nullable=False)
    question4 = Column(Text, nullable=False)
    question5 = Column(Text, nullable=False)
    response = Column(Text, nullable=True)
    cost = Column(Float, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.UTC))
    reading_type = Column(String, nullable=False, default='standard')
    is_answered = Column(Boolean, default=False)
    answer = Column(Text, nullable=True)
    
    user = relationship('User', back_populates='readings')

class Transaction(Base):
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.UTC))
    
    user = relationship('User', back_populates='transactions')

class TopupRequest(Base):
    __tablename__ = 'topup_requests'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    is_approved = Column(Boolean, default=False)
    is_rejected = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(pytz.UTC))
    approved_at = Column(DateTime, nullable=True)
    robokassa_payment_id = Column(String, nullable=True)
    
    user = relationship('User', back_populates='topup_requests')

class Setting(Base):
    __tablename__ = 'settings'
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(String, nullable=False)

Base.metadata.create_all(bind=engine)

def migrate_database():
    from sqlalchemy import text
    db = SessionLocal()
    try:
        try:
            db.execute(text("ALTER TABLE readings ADD COLUMN reading_type VARCHAR NOT NULL DEFAULT 'standard'"))
            db.commit()
        except Exception as e:
            db.rollback()
        
        try:
            db.execute(text("ALTER TABLE readings ADD COLUMN is_answered BOOLEAN DEFAULT 0"))
            db.commit()
        except Exception as e:
            db.rollback()
        
        try:
            db.execute(text("ALTER TABLE readings ADD COLUMN answer TEXT"))
            db.commit()
        except Exception as e:
            db.rollback()
            
        try:
            db.execute(text("ALTER TABLE topup_requests ADD COLUMN robokassa_payment_id VARCHAR"))
            db.commit()
        except Exception as e:
            db.rollback()
            
        try:
            db.execute(text("ALTER TABLE users ADD COLUMN has_used_free_reading BOOLEAN DEFAULT 0"))
            db.commit()
        except Exception as e:
            db.rollback()
    finally:
        db.close()

migrate_database()

def get_reading_cost(db):
    cost = db.query(Setting).filter(Setting.key == 'reading_cost').first()
    if not cost:
        return 100
    return int(float(cost.value))

def set_reading_cost(db, new_cost):
    cost = db.query(Setting).filter(Setting.key == 'reading_cost').first()
    if not cost:
        cost = Setting(key='reading_cost', value=str(new_cost))
    else:
        cost.value = str(new_cost)
    db.add(cost)
    db.commit()

def get_individual_reading_cost(db):
    cost = db.query(Setting).filter(Setting.key == 'individual_reading_cost').first()
    if not cost:
        return 2000
    return int(float(cost.value))

def set_individual_reading_cost(db, new_cost):
    cost = db.query(Setting).filter(Setting.key == 'individual_reading_cost').first()
    if not cost:
        cost = Setting(key='individual_reading_cost', value=str(new_cost))
    else:
        cost.value = str(new_cost)
    db.add(cost)
    db.commit()

def get_palm_reading_cost(db):
    cost = db.query(Setting).filter(Setting.key == 'palm_reading_cost').first()
    if not cost:
        return 1000
    return int(float(cost.value))

def set_palm_reading_cost(db, new_cost):
    cost = db.query(Setting).filter(Setting.key == 'palm_reading_cost').first()
    if not cost:
        cost = Setting(key='palm_reading_cost', value=str(new_cost))
    else:
        cost.value = str(new_cost)
    db.add(cost)
    db.commit()

def get_compatibility_reading_cost(db):
    cost = db.query(Setting).filter(Setting.key == 'compatibility_reading_cost').first()
    if not cost:
        return 300
    return int(float(cost.value))

def set_compatibility_reading_cost(db, new_cost):
    cost = db.query(Setting).filter(Setting.key == 'compatibility_reading_cost').first()
    if not cost:
        cost = Setting(key='compatibility_reading_cost', value=str(new_cost))
    else:
        cost.value = str(new_cost)
    db.add(cost)
    db.commit()

def get_demo_balance(db) -> bool:
    setting = db.query(Setting).filter(Setting.key == 'demo_balance_enabled').first()
    if not setting:
        return True  # по умолчанию демо включено
    return setting.value == '1'

def set_demo_balance(db, enabled: bool):
    setting = db.query(Setting).filter(Setting.key == 'demo_balance_enabled').first()
    if not setting:
        setting = Setting(key='demo_balance_enabled', value='1' if enabled else '0')
    else:
        setting.value = '1' if enabled else '0'
    db.add(setting)
    db.commit()
