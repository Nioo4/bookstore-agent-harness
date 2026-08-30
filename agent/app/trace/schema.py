"""Read-only startup gate for the trace usage migration; no auto-repair."""
from app.trace.errors import TraceDependencyUnavailable


async def require_usage_schema(pool):
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT version,dirty FROM schema_migrations LIMIT 1")
            state = await cursor.fetchone()
            if not state or int(state[0]) < 10 or bool(state[1]):
                raise TraceDependencyUnavailable("trace requires clean migration v10; see migration-recovery.md")
            await cursor.execute(
                "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME IN ('agent_run','agent_step') "
                "AND COLUMN_NAME IN ('prompt_tokens','completion_tokens','cached_tokens','reasoning_tokens','usage_status')")
            columns = {(r[0], r[1]): r[2:] for r in await cursor.fetchall()}
            for table in ('agent_run', 'agent_step'):
                for name in ('prompt_tokens', 'completion_tokens', 'cached_tokens', 'reasoning_tokens'):
                    if columns.get((table, name)) != ('bigint unsigned', 'YES', None):
                        raise TraceDependencyUnavailable("trace usage columns are not v10")
                status = columns.get((table, 'usage_status'))
                if not status or status[0] != 'varchar(24)' or status[1] != 'NO' or status[2] != 'LEGACY_UNKNOWN':
                    raise TraceDependencyUnavailable("trace usage status column is not v10")
            await cursor.execute(
                "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() AND CONSTRAINT_TYPE='CHECK' AND ENFORCED='YES' "
                "AND TABLE_NAME IN ('agent_run','agent_step')")
            names = {r[0] for r in await cursor.fetchall()}
            if not {'ck_agent_run_usage', 'ck_agent_step_usage', 'ck_agent_step_model_usage'} <= names:
                raise TraceDependencyUnavailable("trace usage constraints are not enforced")
        await connection.rollback()
