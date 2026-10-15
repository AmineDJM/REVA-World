from test_db_hardening import *  # noqa
from test_db_hardening import w, tx  # noqa
def test_nonsu_patched(w: dict[str, Any]) -> None:
    """Managed PostgreSQL (e.g. Render) gives a CREATEROLE owner, not a superuser: it may not even
    mention SUPERUSER/CREATEDB/... in CREATE/ALTER ROLE. Bootstrap must still migrate, provision and
    verify the runtime roles, which must end up without privileged attributes or deletion membership."""
    import secrets

    from sqlalchemy import create_engine

    from reva.cli.bootstrap import bootstrap
    from reva.settings import Settings

    db = w["db"]
    owner = f"fx1_owner_{uuid.uuid4().hex[:8]}"
    dbname = f"reva_test_fx1_{uuid.uuid4().hex[:8]}"
    pw = secrets.token_urlsafe(24)
    admin_url = db.settings.migration_url().set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.exec_driver_sql(f"CREATE ROLE \"{owner}\" LOGIN CREATEROLE NOINHERIT PASSWORD '{pw}'")  # url-safe
            c.execute(text(f'CREATE DATABASE "{dbname}" OWNER "{owner}"'))
            # the runtime roles already exist cluster-wide (created by another owner): on a fresh managed
            # cluster the CREATEROLE owner creates them and so holds ADMIN on them; simulate that.
            c.execute(text(f'GRANT reva_api, reva_worker, reva_sim, reva_registry TO "{owner}" WITH ADMIN OPTION'))
        su = create_engine(db.settings.migration_url().set(database=dbname), isolation_level="AUTOCOMMIT")
        with su.connect() as c:  # PostGIS is provisioned by the platform, not by the owner
            c.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        su.dispose()
        url = db.settings.migration_url().set(database=dbname, username=owner, password=pw)
        settings = Settings.model_validate({**db.settings.model_dump(),
                                            "reva_migration_database_url": url.render_as_string(hide_password=False),
                                            "reva_db_name": dbname})
        with create_engine(url).connect() as c:
            assert c.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one() is False
        report = bootstrap(settings)
        assert report["roles"] == ["reva_api", "reva_worker", "reva_sim"]
        with admin.connect() as c:
            attrs = c.execute(text(
                "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication, rolinherit, "
                "rolcanlogin FROM pg_roles WHERE rolname IN ('reva_api','reva_worker','reva_sim')")).all()
            assert len(attrs) == 3
            assert all(not any(r[1:7]) and r[7] for r in attrs), attrs
            members = c.execute(text(
                "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                "JOIN pg_roles g ON g.oid = m.roleid WHERE g.rolname = 'reva_restricted_deletion' "
                "AND r.rolname IN ('reva_api','reva_worker','reva_sim')")).scalars().all()
            assert members == []
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
            c.execute(text(f'DROP ROLE IF EXISTS "{owner}"'))
        admin.dispose()
