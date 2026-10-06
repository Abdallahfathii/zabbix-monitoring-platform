from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import get_settings

settings = get_settings()

# Safety guard: our SQLAlchemy schema must never be created inside the Zabbix
# database (that once replaced Zabbix's own alerts table and broke the
# Problems page with "column a.eventid does not exist").
if settings.POSTGRES_DB.strip().lower() == "zabbix":
    logger.error("SECURITY: Refusing to connect to Zabbix database")
    raise RuntimeError(
        "SECURITY: Refusing to start: POSTGRES_DB/MONITORING_DB points at the Zabbix "
        "database. Set MONITORING_DB=monitoring for python-service."
    )
    raise RuntimeError(
        "Refusing to start: POSTGRES_DB/MONITORING_DB points at the Zabbix "
        "database. Set MONITORING_DB=monitoring for python-service."
    )

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
