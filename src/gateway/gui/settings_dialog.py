"""设置对话框 —— 在 GUI 里配 API 地址 / 密钥 / 模型，不再手改 config.toml。

## 分层（延续 GUI 的纪律：逻辑可测、渲染薄）

    对话框（本文件）      只收集输入 → 产出新的 GatewayConfig + 密钥清单
    config_editor.py     序列化/写环境变量（纯逻辑，有测试）
    gui/app.py           保存编排：写文件 → setx 密钥 → 热重载服务

## 切换供应商时机的坑

QListWidget 的 currentRowChanged 触发时**选择已经变了** —— 如果这时
才把表单写回「当前选中行」，会把 A 家的值写进 B 家。所以用 self._cur
自己记「表单现在显示的是第几家」，写回永远对着 _cur，不问列表。

## 密钥的展示原则

账号表里「密钥」列留空 = 不改动；填了值 = 保存时写入系统环境变量
（不进配置文件）。打开设置页不会泄露任何密钥明文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from ..config import GatewayConfig, ProviderAccount, ProviderConfig

KINDS = ("openai", "anthropic", "gemini", "custom")


@dataclass
class ProviderEd:
    """对话框用的可变编辑模型（和 pydantic 配置互转）。"""

    name: str = ""
    kind: str = "openai"
    base_url: str = ""
    enabled: bool = True
    models: str = ""                       # 逗号分隔，编辑友好
    accounts: list[list[str]] = field(default_factory=list)   # [key_id, key_env, 密钥]


def _from_cfg(cfg: GatewayConfig,
              secrets: dict[str, str] | None = None) -> list[ProviderEd]:
    """配置 + 已存密钥 → 编辑模型。

    密钥明文回显（本地自用，secrets.toml 本来就是明文）—— 打开设置页
    就能看到当前配的是什么，不用凭记忆判断「到底填没填」。
    """
    sec = secrets or {}
    return [
        ProviderEd(
            name=p.name, kind=p.kind, base_url=p.base_url, enabled=p.enabled,
            models=", ".join(p.models),
            accounts=[[a.key_id, a.key_env, sec.get(a.key_env, "")]
                      for a in p.accounts],
        )
        for p in cfg.providers
    ]


def _split_models(text: str) -> list[str]:
    """「a, b，c」→ ['a','b','c']（中英文逗号都认）。"""
    return [m.strip() for m in text.replace("，", ",").split(",") if m.strip()]


class SettingsDialog(QDialog):
    """设置：网关密钥 + 供应商（API 地址/协议/模型/账号）。"""

    def __init__(self, cfg: GatewayConfig, parent=None, *,
                 secrets_path=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("网关设置")
        self.resize(720, 560)
        self._base = cfg
        # 已存密钥：打开就回显；保存时只提交**改动过**的项
        from ..runtime import load_secrets
        self._orig_secrets = load_secrets(secrets_path) if secrets_path else {}
        self._secrets: list[tuple[str, str]] = []
        self._new_cfg: GatewayConfig | None = None
        self._eds = _from_cfg(cfg, self._orig_secrets)
        self._cur = -1                      # 表单当前显示的供应商下标

        root = QVBoxLayout(self)
        root.setSpacing(8)

        lab_keys = QLabel("网关密钥 auth_keys（每行一个，客户端调用时用）")
        lab_keys.setObjectName("dim")
        root.addWidget(lab_keys)
        self.keys_edit = QTextEdit()
        self.keys_edit.setPlainText("\n".join(cfg.auth_keys))
        self.keys_edit.setFixedHeight(56)
        root.addWidget(self.keys_edit)

        mid = QHBoxLayout()
        self.plist = QListWidget()
        self.plist.setFixedWidth(160)
        self.plist.currentRowChanged.connect(self._load_provider)
        mid.addWidget(self.plist)

        form = QFormLayout()
        self.name_ed = QLineEdit()
        self.kind_cb = QComboBox()
        self.kind_cb.addItems(list(KINDS))
        self.url_ed = QLineEdit()
        self.url_ed.setPlaceholderText("https://api.deepseek.com")
        self.models_ed = QLineEdit()
        self.models_ed.setPlaceholderText("deepseek-chat, deepseek-reasoner")
        self.enabled_ck = QCheckBox("启用")
        form.addRow("名称", self.name_ed)
        form.addRow("协议类型", self.kind_cb)
        form.addRow("API 地址", self.url_ed)
        form.addRow("模型（逗号分隔）", self.models_ed)
        form.addRow("", self.enabled_ck)
        mid.addLayout(form, 1)
        root.addLayout(mid)

        lab_acct = QLabel(
            "每行一把 API Key（「加账号」可配多把轮巡）。已保存的 Key 会显示出来，\n"
            "改完保存即生效；Key 存在本机 secrets.toml —— 不进 config.toml / git")
        lab_acct.setObjectName("dim")
        root.addWidget(lab_acct)
        self.acct_table = QTableWidget(0, 2)
        self.acct_table.setHorizontalHeaderLabels(
            ["账号 ID（自动）", "API Key（可选）"])
        self.acct_table.verticalHeader().setVisible(False)
        root.addWidget(self.acct_table, 1)

        acct_btns = QHBoxLayout()
        b_add = QPushButton("加账号")
        b_add.clicked.connect(self._add_account)
        b_del = QPushButton("删选中账号")
        b_del.clicked.connect(self._del_account)
        acct_btns.addWidget(b_add)
        acct_btns.addWidget(b_del)
        acct_btns.addStretch(1)
        root.addLayout(acct_btns)

        btns = QHBoxLayout()
        b_new = QPushButton("新增供应商")
        b_new.clicked.connect(self._add_provider)
        b_rm = QPushButton("删除供应商")
        b_rm.clicked.connect(self._del_provider)
        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._on_save)
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_new)
        btns.addWidget(b_rm)
        btns.addStretch(1)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        root.addLayout(btns)

        self._refresh_list(select=0 if self._eds else -1)

    # ══════════════════════════════════════════════════════════
    # 表单 ⇄ 编辑模型（_cur 是唯一事实源，见模块 docstring）
    # ══════════════════════════════════════════════════════════
    def _refresh_list(self, select: int) -> None:
        self.plist.blockSignals(True)       # 重建列表不触发加载
        self.plist.clear()
        for ed in self._eds:
            self.plist.addItem((ed.name or "（未命名）")
                               + ("" if ed.enabled else "  [停用]"))
        self.plist.blockSignals(False)
        if 0 <= select < len(self._eds):
            self.plist.setCurrentRow(select)
            self._load_provider(select)
        else:
            self._cur = -1
            self._clear_form()

    def _load_provider(self, row: int) -> None:
        self._store_form()                  # 先把旧表单写回旧 _cur
        self._cur = row
        if not (0 <= row < len(self._eds)):
            self._clear_form()
            return
        ed = self._eds[row]
        self.name_ed.setText(ed.name)
        self.kind_cb.setCurrentText(ed.kind)
        self.url_ed.setText(ed.base_url)
        self.models_ed.setText(ed.models)
        self.enabled_ck.setChecked(ed.enabled)
        self._fill_accounts(ed)

    def _clear_form(self) -> None:
        self.name_ed.clear()
        self.kind_cb.setCurrentIndex(0)
        self.url_ed.clear()
        self.models_ed.clear()
        self.enabled_ck.setChecked(False)
        self.acct_table.setRowCount(0)

    def _store_form(self) -> None:
        if not (0 <= self._cur < len(self._eds)):
            return
        ed = self._eds[self._cur]
        ed.name = self.name_ed.text()
        ed.kind = self.kind_cb.currentText()
        ed.base_url = self.url_ed.text()
        ed.models = self.models_ed.text()
        ed.enabled = self.enabled_ck.isChecked()
        # 表格只暴露 账号ID + API Key 两列；存储键（key_env）在内部维护
        rows = []
        for r in range(self.acct_table.rowCount()):
            def cell(c: int) -> str:
                it = self.acct_table.item(r, c)
                return it.text() if it else ""
            old_key_env = ed.accounts[r][1] if r < len(ed.accounts) else ""
            rows.append([cell(0).strip(), old_key_env, cell(1)])
        ed.accounts = rows

    # ── 账号表 ──
    def _fill_accounts(self, ed: ProviderEd) -> None:
        self.acct_table.setRowCount(len(ed.accounts))
        for i, (kid, _key_env, secret) in enumerate(ed.accounts):
            # API Key 明文回显（本地自用；secrets.toml 本来就是明文）
            for col, v in enumerate((kid, secret)):
                self.acct_table.setItem(i, col, QTableWidgetItem(v))

    def _selected_ed(self) -> ProviderEd | None:
        return self._eds[self._cur] if 0 <= self._cur < len(self._eds) else None

    def _add_account(self) -> None:
        self._store_form()
        ed = self._selected_ed()
        if ed is not None:
            n = len(ed.accounts) + 1
            # 变量名自动生成 —— 用户不该关心这个实现细节
            from .config_editor import auto_key_env
            ed.accounts.append([f"key{n}", auto_key_env(ed.name, n), ""])
            self._fill_accounts(ed)

    def _del_account(self) -> None:
        self._store_form()
        ed = self._selected_ed()
        sel = self.acct_table.currentRow()
        if ed is not None and 0 <= sel < len(ed.accounts):
            del ed.accounts[sel]
            self._fill_accounts(ed)

    # ── 供应商增删 ──
    def _add_provider(self) -> None:
        self._store_form()
        name = f"provider-{len(self._eds) + 1}"
        from .config_editor import auto_key_env
        self._eds.append(ProviderEd(
            name=name,
            accounts=[["key1", auto_key_env(name, 1), ""]]))
        self._refresh_list(select=len(self._eds) - 1)

    def _del_provider(self) -> None:
        self._store_form()
        if 0 <= self._cur < len(self._eds):
            del self._eds[self._cur]
            self._refresh_list(select=max(0, self._cur - 1))

    # ── 保存 ──
    def _on_save(self) -> None:
        from .config_editor import auto_key_env

        self._store_form()
        auth_keys = [k.strip() for k in self.keys_edit.toPlainText().splitlines()
                     if k.strip()]

        # 账号归一化：key_id / key_env 留空自动补（用户只该关心 API Key 列），
        # 整行全空 = 没填，丢弃
        normalized: list[list[tuple[str, str, str]]] = []   # [(key_id, key_env, secret)]
        for ed in self._eds:
            rows: list[tuple[str, str, str]] = []
            for i, row in enumerate(ed.accounts, 1):
                kid, kenv, secret = row[0].strip(), row[1].strip(), row[2]
                if not kid and not kenv and not secret:
                    continue
                if not kid:
                    kid = f"key{i}"
                if not kenv:
                    kenv = auto_key_env(ed.name.strip(), i)
                rows.append((kid, kenv, secret))
            normalized.append(rows)

        providers = [
            ProviderConfig(
                name=ed.name.strip() or "unnamed",
                kind=ed.kind,                              # type: ignore[arg-type]
                base_url=ed.base_url.strip(),
                enabled=ed.enabled,
                models=_split_models(ed.models),
                accounts=[
                    ProviderAccount(key_id=kid, key_env=kenv)
                    for kid, kenv, _secret in rows
                ],
            )
            for ed, rows in zip(self._eds, normalized)
        ]
        # 前置校验（比 pydantic 报错更贴着用户的话）：
        # 配了账号但没模型的供应商装配时会被静默跳过 —— 在这就拦下来
        for ed, p in zip(self._eds, providers):
            if p.accounts and not p.models:
                QMessageBox.warning(
                    self, "缺模型清单",
                    f"供应商「{ed.name}」配了账号但没填模型 —— "
                    f"这样它不会参与路由。请补上模型（逗号分隔）。")
                return
        try:
            self._new_cfg = GatewayConfig(
                server=self._base.server, storage=self._base.storage,
                mode=self._base.mode, route=self._base.route,
                jailbreak=self._base.jailbreak, budget=self._base.budget,
                providers=providers, auth_keys=auth_keys,
            )
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "配置不合法", str(exc))
            return
        # 密钥清单：只提交**改动过**的（新建账号填的、或值被改过的）——
        # 回显的旧值原样提交只会白写一遍文件，还会掩盖「没改却报改过」
        self._secrets = [
            (kenv, secret)
            for rows in normalized for _kid, kenv, secret in rows
            if secret and secret != self._orig_secrets.get(kenv)
        ]
        self.accept()

    # ── 给调用方的产物 ──
    def result_config(self) -> GatewayConfig | None:
        """保存成功时的新配置；取消返回 None。"""
        return self._new_cfg

    def result_secrets(self) -> list[tuple[str, str]]:
        """[(key_env, 密钥值), ...] —— 只含本次填了值的。"""
        return self._secrets
