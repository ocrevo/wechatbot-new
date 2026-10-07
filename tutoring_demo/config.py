"""Local configuration; keys and decrypted data never belong in the repository."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DemoConfig:
    db_dir: Path
    account: str
    test_friend: str
    runtime: Path = RUNTIME
    poll_limit: int = 500
    poll_overlap_seq: int = 0

    @property
    def keys_file(self):
        return self.runtime / "keys" / (self.account + ".json")

    @property
    def workdir(self):
        return self.runtime / "decrypted" / self.account

    @property
    def binding(self):
        value = [str(self.db_dir.resolve()), self.account, self.test_friend]
        return sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def configure_runtime():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    os.environ["WECHATAUTO_RUNTIME_DIR"] = str(RUNTIME / "wechatauto")
    os.environ["WECHATAUTO_KEYS_DIR"] = str(RUNTIME / "keys")
    os.environ["WECHATAUTO_RHYTHM"] = "natural"


def load_config(path=None):
    path = Path(path) if path else RUNTIME / "demo-config.json"
    if not path.is_file():
        raise ConfigError("请先运行 configure_demo.ps1，填写数据库父目录、账号目录和测试好友。")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"db_dir", "account", "test_friend"}:
        raise ConfigError("配置只允许 db_dir、account、test_friend 三个字段。")
    for key in data:
        if not isinstance(data[key], str) or not data[key].strip():
            raise ConfigError("配置字段不能为空。")
    account = data["account"].strip()
    if account in {".", ".."} or any(c in account for c in '/\\:'):
        raise ConfigError("account 必须是完整账号目录名，不能包含路径。")
    db_dir = Path(data["db_dir"]).expanduser().resolve()
    if not db_dir.is_dir() or not (db_dir / account / "db_storage").is_dir():
        raise ConfigError("指定账号目录下没有 db_storage；不能使用旧版 WeChat Files 数据库。")
    return DemoConfig(db_dir, account, data["test_friend"].strip())


def load_api_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        path = RUNTIME / "secrets.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            key = data.get("DEEPSEEK_API_KEY", "") if isinstance(data, dict) else ""
    if not isinstance(key, str) or len(key.strip()) < 10 or key.strip() == "public":
        raise ConfigError("未配置有效 DeepSeek 密钥；在本机运行 configure_demo.ps1，不要在聊天中发送密钥。")
    return key.strip()
