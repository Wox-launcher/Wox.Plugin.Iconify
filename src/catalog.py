import base64
import hashlib
import json
import sqlite3
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .icon_svg import IconRecord, is_colorful, iter_icons
from .query_text import escape_like, parse_icon_query

USER_AGENT = "Wox.Plugin.Iconify/0.1.0"
METADATA_URLS = (
    "https://registry.npmjs.org/@iconify/json/latest",
    "https://registry.npmmirror.com/@iconify/json/latest",
)
_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


class DownloadCancelled(Exception):
    pass


class ChecksumError(Exception):
    pass


@dataclass(frozen=True)
class PackageInfo:
    version: str
    tarball_url: str
    integrity: str
    download_size: int


class Progress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "missing"
        self.done = 0
        self.total = 0
        self.error = ""
        self.detail = ""

    def snapshot(self) -> tuple[str, int, int, str, str]:
        with self._lock:
            return (self.phase, self.done, self.total, self.error, self.detail)

    def update(
        self,
        *,
        phase: str | None = None,
        done: int | None = None,
        total: int | None = None,
        error: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            if phase is not None:
                self.phase = phase
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            if error is not None:
                self.error = error
            if detail is not None:
                self.detail = detail


class SingleFlight:
    """Lets one background download own the catalog. Later callers are ignored."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    def try_begin(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            return True

    def end(self) -> None:
        with self._lock:
            self._running = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running


class IconCatalog:
    """Full Iconify collection stored in the plugin cache as a local search index.

    Icon bodies live in a separate table from names so a search does not read
    every SVG off disk. The npm tarball is deleted after the index is ready.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.root = cache_dir / "iconify"
        self.db_path = self.root / "icons.sqlite"
        self._building_path = self.root / "icons.sqlite.building"
        self.tarball_path = self.root / "iconify-json.tgz"
        self._part_path = self.root / "iconify-json.tgz.part"
        self.progress = Progress()
        self.flight = SingleFlight()
        self.icon_count = 0
        self._ready = False
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.RLock()
        self._palette_lock = threading.Lock()
        self._palette_ready = False

    def is_ready(self) -> bool:
        if self._ready and self.db_path.exists():
            return True
        if not self.db_path.exists():
            self._ready = False
            return False
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key = 'ready'").fetchone()
                count_row = conn.execute("SELECT COUNT(*) FROM names").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            self._ready = False
            return False
        count = int(count_row[0]) if count_row else 0
        self.icon_count = count
        self._ready = bool(row and row[0] == "1" and count > 0)
        if self._ready:
            self.progress.update(phase="ready", error="")
        return self._ready

    def close(self) -> None:
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def palette_ready(self) -> bool:
        return self._palette_ready

    def ensure_palette(self) -> None:
        """Add a colorful/monochrome flag to an index that was built before the filter existed."""
        if self._palette_ready or not self.db_path.exists():
            return
        with self._palette_lock:
            if self._palette_ready:
                return
            with self._conn_lock:
                conn = self._connect()
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(names)")}
                if "colorful" not in columns:
                    conn.execute("ALTER TABLE names ADD COLUMN colorful INTEGER NOT NULL DEFAULT -1")
                    conn.commit()
                while True:
                    pending = conn.execute(
                        """
                        SELECT icons.id, icons.body
                        FROM icons
                        JOIN names ON names.id = icons.id
                        WHERE names.colorful = -1
                        LIMIT 400
                        """
                    ).fetchall()
                    if not pending:
                        break
                    conn.executemany(
                        "UPDATE names SET colorful = ? WHERE id = ?",
                        [(1 if is_colorful(str(body)) else 0, icon_id) for icon_id, body in pending],
                    )
                    conn.commit()
            self._palette_ready = True

    def search(self, term: str, limit: int = 100, palette: str = "all") -> list[IconRecord]:
        spec = parse_icon_query(term)
        if spec is None or limit <= 0:
            return []
        self.ensure_palette()

        if spec.prefix and spec.name:
            where = "prefix = ? AND key LIKE ? ESCAPE '\\'"
            where_params: list[object] = [spec.prefix, f"%{escape_like(spec.name)}%"]
            exact = spec.name
        elif spec.prefix:
            where = "prefix = ?"
            where_params = [spec.prefix]
            exact = ""
        else:
            where = " AND ".join("key LIKE ? ESCAPE '\\'" for _ in spec.tokens)
            where_params = [f"%{escape_like(token)}%" for token in spec.tokens]
            exact = spec.text
        if palette == "color":
            where += " AND colorful = 1"
        elif palette == "mono":
            where += " AND colorful = 0"

        params: list[object] = [
            *where_params,
            exact,
            f"{escape_like(exact)}%",
            limit,
        ]
        with self._conn_lock:
            conn = self._connect()
            matched = conn.execute(
                f"""
                SELECT id, prefix, name
                FROM names
                WHERE {where}
                ORDER BY
                    CASE
                        WHEN lower(name) = ? THEN 0
                        WHEN lower(name) LIKE ? ESCAPE '\\' THEN 1
                        ELSE 2
                    END,
                    length(name),
                    prefix,
                    name
                LIMIT ?
                """,
                params,
            ).fetchall()
            if not matched:
                return []

            ids = [row[0] for row in matched]
            placeholders = ",".join("?" for _ in ids)
            stored = {
                row[0]: row
                for row in conn.execute(
                    f"""
                    SELECT id, body, width, height, x, y, rotate, hflip, vflip
                    FROM icons
                    WHERE id IN ({placeholders})
                    """,
                    ids,
                )
            }
            records: list[IconRecord] = []
            for icon_id, prefix, name in matched:
                row = stored.get(icon_id)
                if row is None:
                    continue
                records.append(
                    IconRecord(
                        prefix=str(prefix),
                        name=str(name),
                        body=str(row[1]),
                        width=int(row[2]),
                        height=int(row[3]),
                        left=int(row[4]),
                        top=int(row[5]),
                        rotate=int(row[6]),
                        h_flip=bool(row[7]),
                        v_flip=bool(row[8]),
                    )
                )
            return records

    def get(self, prefix: str, name: str) -> IconRecord | None:
        with self._conn_lock:
            return self._get_locked(prefix, name)

    def _get_locked(self, prefix: str, name: str) -> IconRecord | None:
        conn = self._connect()
        found = conn.execute(
            "SELECT id FROM names WHERE prefix = ? AND name = ?",
            (prefix, name),
        ).fetchone()
        if found is None:
            return None
        row = conn.execute(
            """
            SELECT body, width, height, x, y, rotate, hflip, vflip
            FROM icons
            WHERE id = ?
            """,
            (found[0],),
        ).fetchone()
        if row is None:
            return None
        return IconRecord(
            prefix=prefix,
            name=name,
            body=str(row[0]),
            width=int(row[1]),
            height=int(row[2]),
            left=int(row[3]),
            top=int(row[4]),
            rotate=int(row[5]),
            h_flip=bool(row[6]),
            v_flip=bool(row[7]),
        )

    def download_and_index(self, package: PackageInfo, cancel: threading.Event) -> bool:
        """Download and index once. Returns False when another download already owns the job."""
        if not self.flight.try_begin():
            return False
        try:
            self._download_and_index(package, cancel)
            return True
        finally:
            self.flight.end()

    def index_tarball(self, tar_path: Path, version: str, cancel: threading.Event | None = None) -> None:
        self._index_tarball(tar_path, version, self.progress, cancel or threading.Event())

    def _download_and_index(self, package: PackageInfo, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise DownloadCancelled
        if self.is_ready():
            self.progress.update(phase="ready", error="")
            return
        try:
            if not self._tarball_is_valid(package):
                self.progress.update(phase="downloading", done=0, total=0, detail="", error="")
                self._download_tarball(package, cancel)
            if cancel.is_set():
                raise DownloadCancelled
            self.progress.update(phase="verifying", done=0, total=0, detail="", error="")
            if package.integrity:
                try:
                    verify_integrity(self.tarball_path, package.integrity)
                except ChecksumError:
                    self._delete_archive()
                    raise
            self._index_tarball(self.tarball_path, package.version, self.progress, cancel)
            self._delete_archive()
            self.progress.update(phase="ready", error="", detail="")
        except DownloadCancelled:
            self.progress.update(phase="missing", done=0, total=0, error="", detail="")
            raise
        except Exception as exc:
            self._delete_building()
            self.progress.update(phase="error", error=str(exc))
            raise

    def _tarball_is_valid(self, package: PackageInfo) -> bool:
        if not self.tarball_path.exists() or not package.integrity:
            return False
        try:
            verify_integrity(self.tarball_path, package.integrity)
        except ChecksumError:
            self.tarball_path.unlink(missing_ok=True)
            return False
        return True

    def _download_tarball(self, package: PackageInfo, cancel: threading.Event) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for index, url in enumerate(_tarball_candidates(package.tarball_url)):
            if index > 0 and self._part_path.exists():
                self._part_path.unlink()
            try:
                _download_with_retries(url, self.tarball_path, self._part_path, self.progress, cancel)
                return
            except DownloadCancelled:
                raise
            except Exception as exc:
                last_error = exc
        if last_error is None:
            raise RuntimeError("Iconify package URL is missing")
        raise last_error

    def _index_tarball(self, tar_path: Path, version: str, progress: Progress, cancel: threading.Event) -> None:
        self.close()
        self._delete_building()
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._building_path)
        try:
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE icons (
                    id INTEGER PRIMARY KEY,
                    body TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    x INTEGER NOT NULL,
                    y INTEGER NOT NULL,
                    rotate INTEGER NOT NULL,
                    hflip INTEGER NOT NULL,
                    vflip INTEGER NOT NULL
                );
                CREATE TABLE names (
                    id INTEGER PRIMARY KEY,
                    prefix TEXT NOT NULL,
                    name TEXT NOT NULL,
                    key TEXT NOT NULL,
                    colorful INTEGER NOT NULL
                );
                """
            )
            seen: set[str] = set()
            next_id = 1
            with tarfile.open(tar_path, "r:gz") as tar:
                members = [member for member in tar.getmembers() if _is_icon_set_member(member)]
                progress.update(phase="indexing", done=0, total=len(members), detail="", error="")
                for index, member in enumerate(members, start=1):
                    if cancel.is_set():
                        raise DownloadCancelled
                    prefix_hint = Path(member.name).stem
                    progress.update(done=index - 1, detail=prefix_hint)
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        progress.update(done=index)
                        continue
                    try:
                        data = json.loads(extracted.read())
                    except json.JSONDecodeError:
                        progress.update(done=index)
                        continue
                    if not isinstance(data, dict):
                        progress.update(done=index)
                        continue
                    icon_rows: list[tuple] = []
                    name_rows: list[tuple] = []
                    for record in iter_icons(data, prefix_hint):
                        identity = f"{record.prefix}:{record.name}"
                        if identity in seen:
                            continue
                        seen.add(identity)
                        icon_rows.append(
                            (
                                next_id,
                                record.body,
                                record.width,
                                record.height,
                                record.left,
                                record.top,
                                record.rotate,
                                int(record.h_flip),
                                int(record.v_flip),
                            )
                        )
                        name_rows.append((next_id, record.prefix, record.name, identity.casefold(), 1 if is_colorful(record.body) else 0))
                        next_id += 1
                    if icon_rows:
                        conn.executemany(
                            "INSERT INTO icons (id, body, width, height, x, y, rotate, hflip, vflip) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            icon_rows,
                        )
                        conn.executemany(
                            "INSERT INTO names (id, prefix, name, key, colorful) VALUES (?, ?, ?, ?, ?)",
                            name_rows,
                        )
                        conn.commit()
                    progress.update(done=index, detail=prefix_hint)

            count = next_id - 1
            if count <= 0:
                raise RuntimeError("The archive did not contain any icons")
            conn.execute("CREATE INDEX idx_names_prefix_name ON names(prefix, name)")
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("ready", "1"), ("icons", str(count)), ("version", version)],
            )
            conn.commit()
        except Exception:
            conn.close()
            self._delete_building()
            raise
        else:
            conn.close()

        self._building_path.replace(self.db_path)
        self.icon_count = next_id - 1
        self._ready = True

    def _connect(self) -> sqlite3.Connection:
        with self._conn_lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            return self._conn

    def _delete_building(self) -> None:
        self._building_path.unlink(missing_ok=True)

    def _delete_archive(self) -> None:
        for path in (self.tarball_path, self._part_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def fetch_package_info() -> PackageInfo:
    errors: list[str] = []
    for url in METADATA_URLS:
        try:
            payload = json.loads(_read_url(url, timeout=20))
            dist = payload["dist"]
            tarball = dist["tarball"]
            if not isinstance(tarball, str) or not tarball:
                raise RuntimeError("package metadata has no tarball")
            download_size, resolved_url = _tarball_download_size(tarball)
            return PackageInfo(
                version=str(payload.get("version") or ""),
                tarball_url=resolved_url or tarball,
                integrity=str(dist.get("integrity") or ""),
                download_size=download_size,
            )
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("Unable to resolve the Iconify icon package. " + "; ".join(errors))


def verify_integrity(path: Path, integrity: str) -> None:
    algo, separator, digest = integrity.partition("-")
    if not separator or algo != "sha512" or not digest:
        return
    expected = base64.b64decode(digest)
    hasher = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.digest() != expected:
        raise ChecksumError("Downloaded archive checksum did not match")


def content_length_from_headers(headers: object) -> int:
    """Read the archive size. A range response puts the full length in Content-Range."""
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return 0
    content_range = str(getter("Content-Range") or "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    length = str(getter("Content-Length") or "")
    if length.isdigit():
        return int(length)
    return 0


def format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _tarball_download_size(url: str) -> tuple[int, str]:
    last_error: Exception | None = None
    for candidate in _tarball_candidates(url):
        try:
            size = _probe_content_length(candidate)
        except Exception as exc:
            last_error = exc
            continue
        if size > 0:
            return size, candidate
    if last_error is not None:
        return 0, url
    return 0, url


def _probe_content_length(url: str) -> int:
    head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(head, timeout=20) as response:
            size = content_length_from_headers(response.headers)
            if size > 0:
                return size
    except urllib.error.HTTPError:
        pass

    ranged = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
    with urllib.request.urlopen(ranged, timeout=20) as response:
        return content_length_from_headers(response.headers)


def _tarball_candidates(url: str) -> list[str]:
    urls = [url]
    mirror = url.replace("://registry.npmjs.org/", "://registry.npmmirror.com/")
    if mirror != url:
        urls.append(mirror)
    return urls


def _download_with_retries(
    url: str,
    dest: Path,
    part: Path,
    progress: Progress,
    cancel: threading.Event,
) -> None:
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(3):
        if cancel.is_set():
            raise DownloadCancelled
        try:
            _download_one(url, dest, part, progress, cancel)
            return
        except DownloadCancelled:
            raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in _RETRYABLE_HTTP:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < 2:
            _sleep(delay, cancel)
            delay *= 2
    if last_error is None:
        raise RuntimeError(f"Failed to download {url}")
    raise last_error


def _download_one(url: str, dest: Path, part: Path, progress: Progress, cancel: threading.Event) -> None:
    existing = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if existing:
        request.add_header("Range", f"bytes={existing}-")
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing > 0:
            part.replace(dest)
            progress.update(done=existing, total=existing)
            return
        raise

    with response:
        status = getattr(response, "status", None) or response.getcode()
        length_header = response.headers.get("Content-Length")
        length = int(length_header) if length_header and length_header.isdigit() else 0
        if status == 206:
            total = existing + length
            mode = "ab"
            done = existing
        else:
            total = length
            mode = "wb"
            done = 0
        progress.update(done=done, total=total)
        with part.open(mode) as handle:
            while True:
                if cancel.is_set():
                    raise DownloadCancelled
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                progress.update(done=done)
    part.replace(dest)


def _read_url(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _sleep(seconds: float, cancel: threading.Event) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel.is_set():
            raise DownloadCancelled
        time.sleep(min(0.2, deadline - time.monotonic()))


def _is_icon_set_member(member: tarfile.TarInfo) -> bool:
    if not member.isfile() or not member.name.endswith(".json"):
        return False
    parts = member.name.replace("\\", "/").split("/")
    if ".." in parts or len(parts) < 2:
        return False
    return parts[-2] == "json"
