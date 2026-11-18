"""数据备份与恢复（2026-11-18）。

`.data/` 是用户的生产数据：几个月采集积累的岗位、证据、报告与对话全部在
`jobscope.sqlite3` 一个文件里（浏览器工作目录可再生，不入备份）。

设计要点：
- 备份用 SQLite 在线备份 API（`Connection.backup`）生成一致快照，不需要停服务，
  也不直接复制 WAL 模式下的库文件（那样可能拿到撕裂的中间态）；
- 快照打成单文件 zip 存放在 `.data/backups/`，按保留上限裁旧（恢复前的安全备份
  优先保留）；
- 恢复走同一条在线备份通道（备份文件 → 活库），恢复前先对当前库做一份
  「恢复前」安全备份，手滑选错文件也能退回来；
- 恢复只认「zip 内恰好一个 SQLite 库」的备份包，解开先做 `quick_check`，
  不是本项目生成的库文件一律拒绝，绝不盲写活库。
"""

import io
import re
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .repository import Repository

RETENTION = 10  # 常规备份保留份数；「恢复前」安全备份另计
SQLITE_HEADER = b"SQLite format 3\x00"
BACKUP_ID_PATTERN = re.compile(r"^jobscope-[A-Za-z0-9_-]+$")


class BackupError(ValueError):
    """面向用户的失败原因（端点层转 422/400）。"""


def _now_stamp() -> str:
    # 含微秒：同一秒内连点「立即备份」也保持文件名与时序唯一，保留裁剪不会删错方向。
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


class BackupStore:
    def __init__(self, repo: Repository, data_dir: Path):
        self.repo = repo
        self.data_dir = Path(data_dir)
        self.backups_dir = self.data_dir / "backups"
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    # ---- 备份 ----

    def create(self, label: str = "") -> dict:
        """对活库做一致快照并落盘为 zip；返回该份备份的元数据。"""
        stamp = _now_stamp()
        backup_id = f"jobscope-{label + '-' if label else ''}{stamp}"
        target = self._path(backup_id)
        if target.exists():  # 同秒内重复点按钮：补一个后缀而不是覆盖既有备份
            suffix = 1
            while self._path(f"{backup_id}-{suffix}").exists():
                suffix += 1
            backup_id = f"{backup_id}-{suffix}"
            target = self._path(backup_id)
        # 注意：sqlite3 连接的 with 只管事务不关句柄；Windows 下句柄不关，临时文件删不掉。
        try:
            with closing(sqlite3.connect(self.repo.path, timeout=30)) as snapshot, \
                    closing(sqlite3.connect(target.with_suffix(".tmp"))) as dest:
                snapshot.backup(dest)
        except sqlite3.Error as exc:
            target.with_suffix(".tmp").unlink(missing_ok=True)
            raise BackupError(f"生成快照失败（活库暂时被占用，稍后再试）：{exc}") from None
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("jobscope.sqlite3", target.with_suffix(".tmp").read_bytes())
        target.with_suffix(".tmp").unlink(missing_ok=True)
        self._prune()
        return self.info(backup_id)

    def info(self, backup_id: str) -> dict:
        path = self._path(backup_id)
        stat = path.stat()
        return {
            "id": backup_id,
            "name": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "pre_restore": "pre-restore" in backup_id,
        }

    def list(self) -> list[dict]:
        items = []
        for path in self.backups_dir.glob("jobscope-*.zip"):
            backup_id = path.stem
            if not BACKUP_ID_PATTERN.match(backup_id):
                continue
            items.append(self.info(backup_id))
        return sorted(items, key=lambda item: (item["created_at"], item["name"]), reverse=True)

    def status(self) -> dict:
        """今日概览与设置页共用：最新备份时间、距今天数、现有份数。"""
        backups = self.list()
        last = backups[0]["created_at"] if backups else None
        days = None
        if last:
            try:
                days = round((datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 86400, 1)
            except ValueError:
                days = None
        return {"backups": backups, "last_backup_at": last, "days_since": days, "retention": RETENTION}

    def read(self, backup_id: str) -> bytes:
        path = self._path(backup_id)
        if not path.exists():
            raise BackupError("备份不存在或已删除")
        return path.read_bytes()

    def delete(self, backup_id: str) -> None:
        path = self._path(backup_id)
        if not path.exists():
            raise BackupError("备份不存在或已删除")
        path.unlink()

    # ---- 恢复 ----

    def restore(self, data: bytes) -> dict:
        """用上传的备份包覆盖活库。恢复前先落一份「恢复前」安全备份。

        只接受本模块 create 产出的结构：zip 内恰好一个 `jobscope.sqlite3`。
        备份内容在内存/临时文件中校验（SQLite 文件头 + quick_check），通过才动活库；
        全程不按原名解包，避免任何路径覆盖到活库。
        """
        limit = 2 * 1024 * 1024 * 1024
        if len(data) > limit:
            raise BackupError("备份包超过 2 GB，拒绝处理")
        snapshot = self.data_dir / f"restore-{_now_stamp()}.sqlite3"
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if names != ["jobscope.sqlite3"]:
                    raise BackupError("备份包结构不符：应为仅含 jobscope.sqlite3 的 zip（由「立即备份」导出）")
                snapshot.write_bytes(archive.read("jobscope.sqlite3"))
            if snapshot.read_bytes()[:16] != SQLITE_HEADER:
                raise BackupError("这不是有效的 SQLite 数据库文件")
            # quick_check 在只读连接上跑：损坏的备份在这里拦下，不进活库。
            try:
                with closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True, timeout=30)) as check:
                    result = check.execute("PRAGMA quick_check").fetchone()
                    if not result or result[0] != "ok":
                        raise BackupError(f"备份完整性校验未通过：{result[0] if result else '无法读取'}")
            except sqlite3.DatabaseError as exc:
                raise BackupError(f"备份无法作为数据库打开：{exc}") from None
            safety = self.create(label="pre-restore")
            with closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True, timeout=30)) as source, \
                    closing(sqlite3.connect(self.repo.path, timeout=60)) as dest:
                source.backup(dest)
            counts = {"jobs": 0, "evidence": 0}
            with closing(sqlite3.connect(f"file:{self.repo.path}?mode=ro", uri=True)) as verify:
                counts["jobs"] = verify.execute("SELECT count(*) FROM jobs").fetchone()[0]
                counts["evidence"] = verify.execute("SELECT count(*) FROM evidence").fetchone()[0]
            return {"restored": True, "safety_backup": safety["name"], **counts}
        except zipfile.BadZipFile:
            raise BackupError("无法读取备份包：不是有效的 zip 文件") from None
        finally:
            snapshot.unlink(missing_ok=True)

    # ---- 内部 ----

    def _path(self, backup_id: str) -> Path:
        if not BACKUP_ID_PATTERN.match(backup_id):
            raise BackupError("备份编号不合法")
        path = (self.backups_dir / f"{backup_id}.zip").resolve()
        if path.parent != self.backups_dir.resolve():
            raise BackupError("备份编号不合法")
        return path

    def _prune(self) -> None:
        """超出保留份数时裁最旧的常规备份；「恢复前」安全备份不占常规名额。"""
        regular = [item for item in self.list() if not item["pre_restore"]]
        for item in regular[RETENTION:]:
            self.delete(item["id"])
