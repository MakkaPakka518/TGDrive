"""Private account registry and resumable media metadata index."""
import json
import os
import secrets
import sqlite3
import time

PAGE_SIZE = 30


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, user_id TEXT UNIQUE, name TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '', phone_masked TEXT NOT NULL DEFAULT '',
                session BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL, title TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL, peer TEXT NOT NULL, token TEXT NOT NULL,
                cursor INTEGER NOT NULL DEFAULT 0, latest_id INTEGER NOT NULL DEFAULT 0,
                history_done INTEGER NOT NULL DEFAULT 0, last_sync REAL NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                UNIQUE(account_id, chat_id)
            );
            CREATE TABLE IF NOT EXISTS media (
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL, date TEXT NOT NULL, info TEXT NOT NULL,
                search_text TEXT NOT NULL, PRIMARY KEY(source_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS media_date ON media(date DESC, source_id, message_id DESC);
        """)

    def close(self):
        self.db.close()

    def import_legacy(self, encrypted_session=None, config=None):
        if self.db.execute("SELECT 1 FROM meta WHERE key='legacy_imported'").fetchone():
            return
        with self.db:
            if encrypted_session:
                account_id = secrets.token_hex(12)
                self.db.execute("INSERT INTO accounts(id,name,session,created) VALUES(?,?,?,?)", (account_id, "已有账号", encrypted_session, time.time()))
                config = config or {}
                if config.get("channel_id"):
                    self.db.execute("INSERT INTO sources(id,account_id,chat_id,title,kind,peer,token) VALUES(?,?,?,?,?,?,?)", (
                        secrets.token_hex(12), account_id, int(config["channel_id"]), config.get("title") or "已有网盘", "channel", "{}", secrets.token_hex(16),
                    ))
            self.db.execute("INSERT INTO meta(key,value) VALUES('legacy_imported','1')")

    def account(self, account_id):
        row = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def account_by_user(self, user_id):
        row = self.db.execute("SELECT * FROM accounts WHERE user_id=?", (str(user_id),)).fetchone()
        return dict(row) if row else None

    def accounts(self):
        return [dict(row) for row in self.db.execute("SELECT id,user_id,name,username,phone_masked,status,error,(SELECT COUNT(*) FROM sources WHERE account_id=accounts.id) AS source_count FROM accounts ORDER BY created")]

    def save_account(self, account_id, user, encrypted_session):
        with self.db:
            self.db.execute("""INSERT INTO accounts(id,user_id,name,username,phone_masked,session,created)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,
                name=excluded.name,username=excluded.username,phone_masked=excluded.phone_masked,
                session=excluded.session,status='ready',error=''""", (
                account_id, str(user["id"]), user["name"], user.get("username", ""), user.get("phone_masked", ""), encrypted_session, time.time(),
            ))

    def account_error(self, account_id, error, status="error"):
        with self.db:
            self.db.execute("UPDATE accounts SET status=?,error=? WHERE id=?", (status, error, account_id))

    def remove_account(self, account_id):
        with self.db:
            self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def source(self, source_id):
        row = self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["peer"] = json.loads(result["peer"])
        return result

    def sources(self):
        return [dict(row) for row in self.db.execute("""SELECT s.id,s.account_id,s.chat_id,s.title,s.username,s.kind,
            s.history_done,s.last_sync,s.retry_at,s.error,a.name AS account_name,a.status AS account_status,
            (SELECT COUNT(*) FROM media WHERE source_id=s.id) AS media_count
            FROM sources s JOIN accounts a ON a.id=s.account_id ORDER BY a.created,s.title""")]

    def add_source(self, account_id, chat_id, title, username, kind, peer):
        existing = self.db.execute("SELECT id FROM sources WHERE account_id=? AND chat_id=?", (account_id, chat_id)).fetchone()
        source_id = existing[0] if existing else secrets.token_hex(12)
        with self.db:
            self.db.execute("""INSERT INTO sources(id,account_id,chat_id,title,username,kind,peer,token)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id,chat_id) DO UPDATE SET
                title=excluded.title,username=excluded.username,kind=excluded.kind,peer=excluded.peer""", (
                source_id, account_id, chat_id, title, username, kind, json.dumps(peer), secrets.token_hex(16),
            ))
        return source_id

    def update_peer(self, source_id, peer):
        with self.db:
            self.db.execute("UPDATE sources SET peer=? WHERE id=?", (json.dumps(peer), source_id))

    def remove_source(self, source_id):
        with self.db:
            self.db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def source_error(self, source_id, error, retry_at=0):
        with self.db:
            self.db.execute("UPDATE sources SET error=?,retry_at=?,last_sync=? WHERE id=?", (error, retry_at, time.time(), source_id))

    def index_batch(self, source_id, items, latest_id=0, cursor=None, done=None):
        if not self.source(source_id):
            return
        with self.db:
            for item in items:
                search = " ".join(str(item.get(key, "")) for key in ["title", "file_name", "description", "performer"]).casefold()
                self.db.execute("""INSERT INTO media(source_id,message_id,date,info,search_text) VALUES(?,?,?,?,?)
                    ON CONFLICT(source_id,message_id) DO UPDATE SET date=excluded.date,info=excluded.info,search_text=excluded.search_text""", (
                    source_id, item["id"], item.get("date", ""), json.dumps(item, ensure_ascii=False), search,
                ))
            self.db.execute("UPDATE sources SET latest_id=MAX(latest_id,?),last_sync=?,error='',retry_at=0 WHERE id=?", (latest_id, time.time(), source_id))
            if cursor is not None:
                self.db.execute("UPDATE sources SET cursor=? WHERE id=?", (cursor, source_id))
            if done is not None:
                self.db.execute("UPDATE sources SET history_done=? WHERE id=?", (int(done), source_id))

    def reset_source(self, source_id):
        with self.db:
            self.db.execute("DELETE FROM media WHERE source_id=?", (source_id,))
            self.db.execute("UPDATE sources SET cursor=0,latest_id=0,history_done=0,last_sync=0,error='' WHERE id=?", (source_id,))

    def remove_media(self, source_id, message_id):
        with self.db:
            self.db.execute("DELETE FROM media WHERE source_id=? AND message_id=?", (source_id, message_id))

    def catalog(self, page=1, source_id="", query=""):
        where, params = [], []
        if source_id:
            where.append("m.source_id=?")
            params.append(source_id)
        if query:
            where.append("m.search_text LIKE ? ESCAPE '\\'")
            params.append("%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = self.db.execute("SELECT COUNT(*) FROM media m" + clause, params).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, pages))
        rows = self.db.execute("""SELECT m.info,m.source_id,s.title AS source_title,s.account_id
            FROM media m JOIN sources s ON m.source_id=s.id""" + clause +
            " ORDER BY m.date DESC,m.source_id,m.message_id DESC LIMIT ? OFFSET ?", params + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
        items = [{**json.loads(row["info"]), "source_id": row["source_id"], "source_title": row["source_title"], "account_id": row["account_id"]} for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": PAGE_SIZE, "pages": pages, "has_next": page < pages}
