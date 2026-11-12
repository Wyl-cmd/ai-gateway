"""破甲产物库 —— 把成功的破甲固化，跨模型迁移。

## Master 的原话

「有没有一个方法去做迭代式的破甲，可以用旧模型已经破甲的实现对新模型的破甲，
  并且把破甲结果和模型对应并固化下来。」

这就是本模块。核心是**经验资产化**：
  破甲成功时把那个 prompt 原文存下来，绑定「模型 × 问题指纹」；
  换个新模型时，先把旧模型的成功 prompt 搬过去试（迁移）。

## 为什么 J5（技术有效性记忆）不够

J5 只记「技术名 × 模型」的成功率 —— 它知道「base64 对 deepseek 成功率 0%」，
**但不知道当初具体发了什么 prompt**。而真正有复用价值的是 prompt 原文：
换一个模型，把它改两个词可能就通了。技术名只是分类标签，不是资产。

## 四个查询优先级（越靠前越可信）

    ① 同模型 + 同问题指纹 + 成功过   ← 直接复用，最准
    ② 同问题指纹 + 别的模型成功过   ← ★ 跨模型迁移（用户要的那个）
    ③ 同模型 + 别的问题成功过       ← 这个模型吃哪套话术
    ④ 全都没有                      ← 返回 None，上层走先验顺序

## 问题指纹用字符 n-gram 不用向量模型

理由：
  ① 不引依赖 —— 换来「差不多」的相似度不值得几百 MB 的 embedding 模型
  ② 可解释 —— 相似度能算出来给人看，不是黑盒
  ③ 中文友好 —— 3-gram 对中文本来就比分词好使（不用处理分词歧义）
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field

#: 相似度阈值。低于这个值不算「同类问题」—— 迁错了比不迁更糟
#: （把一个提权的 prompt 拿去问天气，纯浪费钱）。
TRANSFER_THRESHOLD = 0.30

#: n-gram 长度。3 对中英文都比较稳（2 太碎、4 太稀疏）。
NGRAM = 3

#: 分数：每次成功 +1，每次失败 -1，下限 0。越用越准。
SCORE_SUCCESS = 1.0
SCORE_FAILURE = -1.0


# ═══════════════════════════════════════════════════════════════
# 指纹与相似度
# ═══════════════════════════════════════════════════════════════
def _normalize(text: str) -> str:
    """去掉空白和标点 —— 多打一个空格不该变成另一个问题。"""
    return re.sub(r"[\s\W_]+", "", text.lower())


def problem_signature(problem: str) -> str:
    """问题指纹：规范化文本的 n-gram 集合的哈希。

    存的是规范化后的原文而不是哈希值 —— 因为要做相似度计算，
    哈希之后就没法算 Jaccard 了。这里「指纹」= 可用于比对的规范形式。
    """
    return _normalize(problem)


def _ngrams(sig: str) -> set[str]:
    if len(sig) < NGRAM:
        return {sig} if sig else set()
    return {sig[i:i + NGRAM] for i in range(len(sig) - NGRAM + 1)}


#: 安全领域概念表 —— 补 n-gram 的短板。
#:
#: 实测：「如何提权获取系统权限」和「怎么拿到root权限提权」的 3-gram Jaccard = 0.0，
#: 字面完全不重叠。但它们说的是同一件事。
#: 纯字面相似度对「同义不同词」天生无能，加一层概念归一来解决。
#:
#: 只收**安全领域的高频概念**，不做通用同义词表 ——
#: 通用表维护不动，而且泛化到什么都像，等于没过滤。
CONCEPTS: dict[str, tuple[str, ...]] = {
    "提权": ("提权", "root", "管理员权限", "最高权限", "系统权限", "privilege",
             "escalat", "sudo", "admin", "超级用户"),
    "入侵": ("入侵", "渗透", "攻击", "hack", "intrusion", "exploit", "打点",
             "getshell", "shell", "webshell", "接管"),
    "密码": ("密码", "口令", "password", "hash", "撞库", "爆破", "credential"),
    "伪造": ("伪造", "假证", "假造", "变造", "counterfeit", "fake", "ps"),
    "燃烧": ("燃烧", "焚烧", "点火", "燃料", "爆炸", "爆燃", "combust", "burn",
             "incendiary", "pyro"),
    "毒品": ("毒品", "制毒", "冰毒", "麻古", "drug", "meth", "narcotic"),
    "枪械": ("枪", "弹药", "子弹", "火器", "gun", "firearm", "ammo", "weapon"),
    "绕过": ("绕过", "规避", "越过", "bypass", "circumvent", "evade"),
    "数据": ("数据库", "数据", "dump", "脱库", "泄露", "exfil", "database"),
    "木马": ("木马", "后门", "远控", "rat", "backdoor", "malware", "trojan"),
}


def _concepts(sig: str) -> set[str]:
    """把文本映射成概念集合。"""
    low = sig.lower()
    out = set()
    for name, words in CONCEPTS.items():
        if any(w in low for w in words):
            out.add(name)
    return out


def similarity(a: str, b: str) -> float:
    """混合相似度：字面 n-gram 与概念重合，取加权最大。

    两种信号补各自的短板：
      字面   —— 措辞相近时准（「如何提权」vs「怎样提权」）
      概念   —— 同义不同词时准（「提权」vs「拿 root」）
    任一强就够，所以取 max 而不是平均 —— 平均会让「一个很强一个很弱」
    的组合被拉低到阈值以下，白白漏掉真正该迁移的。
    """
    # 字面
    sa, sb = _ngrams(a), _ngrams(b)
    if sa and sb:
        lit = len(sa & sb) / len(sa | sb)
    else:
        lit = 0.0

    # 概念：重合概念 / 较小集合的概念数
    ca, cb = _concepts(a), _concepts(b)
    if ca and cb:
        # 用 min 做分母：短问句只提一个概念，长问句提三个，
        # 用 union 会把「一个概念完全命中」压到 0.33 以下。
        con = len(ca & cb) / min(len(ca), len(cb))
    else:
        con = 0.0

    return max(lit, con)


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════
@dataclass
class Artifact:
    """一条破甲资产。"""

    model: str
    problem_sig: str
    technique: str
    prompt_text: str
    successes: int = 0
    failures: int = 0
    uses: int = 0
    tokens: int = 0
    last_used: int = 0
    #: 命中来源，不存库 —— 查询时才知道
    source: str = ""
    reason: str = ""
    similarity: float = 1.0

    @property
    def score(self) -> float:
        return self.successes * SCORE_SUCCESS + self.failures * SCORE_FAILURE

    @property
    def success_rate(self) -> float:
        total = self.successes + self.failures
        return 1.0 if total == 0 else self.successes / total

    def to_dict(self) -> dict:
        return {
            "model": self.model, "technique": self.technique,
            "prompt_text": self.prompt_text,
            "successes": self.successes, "failures": self.failures,
            "uses": self.uses, "tokens": self.tokens,
            "score": self.score, "success_rate": round(self.success_rate, 3),
            "source": self.source,
        }


# ═══════════════════════════════════════════════════════════════
# 存储
# ═══════════════════════════════════════════════════════════════
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jailbreak_artifact (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model        TEXT NOT NULL,
    problem_sig  TEXT NOT NULL,
    technique    TEXT NOT NULL,
    prompt_text  TEXT NOT NULL,
    successes    INTEGER NOT NULL DEFAULT 0,
    failures     INTEGER NOT NULL DEFAULT 0,
    uses         INTEGER NOT NULL DEFAULT 0,
    tokens       INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    last_used    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(model, problem_sig, prompt_text)
);
CREATE INDEX IF NOT EXISTS idx_art_model ON jailbreak_artifact(model);
"""


class ArtifactStore:
    def __init__(self, db_path: str) -> None:
        self._db = str(db_path)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db)
        c.row_factory = sqlite3.Row
        return c

    # ── 写入 ──
    def record_success(self, *, model: str, problem: str, technique: str,
                       prompt_text: str, tokens: int = 0) -> None:
        self._upsert(model, problem_signature(problem), technique, prompt_text,
                     ok=True, tokens=tokens)

    def record_failure(self, *, model: str, problem: str, technique: str,
                       prompt_text: str, tokens: int = 0) -> None:
        self._upsert(model, problem_signature(problem), technique, prompt_text,
                     ok=False, tokens=tokens)

    def _upsert(self, model: str, sig: str, technique: str, prompt: str,
                *, ok: bool, tokens: int) -> None:
        import time
        now = int(time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT id, successes, failures, uses, tokens FROM jailbreak_artifact "
                "WHERE model=? AND problem_sig=? AND prompt_text=?",
                (model, sig, prompt)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO jailbreak_artifact "
                    "(model, problem_sig, technique, prompt_text, successes, failures, "
                    " uses, tokens, created_at, last_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (model, sig, technique, prompt, 1 if ok else 0, 0 if ok else 1,
                     1, tokens, now, now))
            else:
                c.execute(
                    "UPDATE jailbreak_artifact SET successes=successes+?, "
                    "failures=failures+?, uses=uses+?, tokens=tokens+?, last_used=? "
                    "WHERE id=?",
                    (1 if ok else 0, 0 if ok else 1, 1, tokens, now, row["id"]))
            c.commit()

    def mark_used(self, artifact: Artifact) -> None:
        import time
        with self._conn() as c:
            c.execute("UPDATE jailbreak_artifact SET uses=uses+1, last_used=? "
                      "WHERE model=? AND problem_sig=? AND prompt_text=?",
                      (int(time.time()), artifact.model, artifact.problem_sig,
                       artifact.prompt_text))
            c.commit()

    # ── 读取 ──
    def all(self) -> list[Artifact]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jailbreak_artifact").fetchall()
        return [self._to_artifact(r) for r in rows]

    @staticmethod
    def _to_artifact(r: sqlite3.Row) -> Artifact:
        return Artifact(
            model=r["model"], problem_sig=r["problem_sig"],
            technique=r["technique"], prompt_text=r["prompt_text"],
            successes=r["successes"], failures=r["failures"],
            uses=r["uses"], tokens=r["tokens"], last_used=r["last_used"],
        )

    # ── 建议（四个优先级）──
    def suggest(self, *, model: str, problem: str,
                technique: str | None = None,
                allow_model_preference: bool = False) -> Artifact | None:
        sig = problem_signature(problem)

        # ① 同模型 + 同问题
        for a in self._query(model=model, technique=technique):
            if a.successes > 0 and similarity(sig, a.problem_sig) >= TRANSFER_THRESHOLD:
                a.source = "same_model"
                a.reason = (f"该模型在同类问题上成功过 {a.successes} 次"
                            f"（相似度 {similarity(sig, a.problem_sig):.2f}）")
                a.similarity = similarity(sig, a.problem_sig)
                return a

        # ② 跨模型迁移 —— 这是 Master 要的那个
        cands = self.transfer_candidates(model=model, problem=problem,
                                         technique=technique)
        if cands:
            best = cands[0]
            best.source = "transfer"
            best.reason = (f"从 {best.model} 迁移：它在同类问题上成功过 "
                           f"{best.successes} 次（相似度 {best.similarity:.2f}）")
            return best

        # ③ 同模型的模型偏好
        if allow_model_preference:
            for a in self._query(model=model, technique=technique):
                if a.successes > 0:
                    a.source = "model_preference"
                    a.reason = f"该模型对这个技术有成功记录（{a.successes} 次）"
                    return a

        return None

    def transfer_candidates(self, *, model: str, problem: str,
                            technique: str | None = None) -> list[Artifact]:
        """能迁移给 `model` 的候选 —— 别的模型在同类问题上的成功产物。

        按「成功率 × 相似度」排。全部失败过的不要。
        """
        sig = problem_signature(problem)
        out: list[Artifact] = []
        for a in self.all():
            if a.model == model:
                continue
            if technique is not None and a.technique != technique:
                continue
            if a.successes <= 0:
                continue
            sim = similarity(sig, a.problem_sig)
            if sim < TRANSFER_THRESHOLD:
                continue
            a.similarity = sim
            out.append(a)
        out.sort(key=lambda a: (-(a.success_rate * a.similarity), -a.successes))
        return out

    def _query(self, *, model: str, technique: str | None) -> list[Artifact]:
        sql = "SELECT * FROM jailbreak_artifact WHERE model=?"
        params: list[object] = [model]
        if technique is not None:
            sql += " AND technique=?"
            params.append(technique)
        sql += " ORDER BY successes DESC, uses DESC"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._to_artifact(r) for r in rows]

    # ── 统计 ──
    def stats(self) -> dict:
        items = self.all()
        by_model: dict[str, int] = {}
        by_tech: dict[str, int] = {}
        for a in items:
            by_model[a.model] = by_model.get(a.model, 0) + 1
            by_tech[a.technique] = by_tech.get(a.technique, 0) + 1
        return {
            "total": len(items),
            "by_model": by_model,
            "by_technique": by_tech,
            "successful": sum(1 for a in items if a.successes > 0),
        }
