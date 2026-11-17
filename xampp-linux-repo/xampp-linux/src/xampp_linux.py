#!/usr/bin/env python3
"""XAMPP Control Panel para Linux (.deb) — Apache + MySQL + PHP + phpMyAdmin (+ Tomcat).

Réplica del panel de control de XAMPP para Windows, pensada para distribuciones con paquetes .deb.
Todo corre como tu usuario (sin root) y vive en ~/.local/share/xampp-linux:
  htdocs/   -> tus proyectos web (como C:\\xampp\\htdocs)
  mysql/    -> datos de MySQL
  logs/     -> registros
"""
import glob
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QProcess, QProcessEnvironment, QRect, QSize, QTimer, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QIcon, QPainter
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QHeaderView,
)

VERSION = "3.3.0"
COMPILED = "Apr 6th 2021"
TITLE = f"XAMPP Control Panel v{VERSION}   [ Compiled: {COMPILED} ]"
BASE = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "xampp-linux"
HTDOCS, LOGS, RUN, CONF = BASE / "htdocs", BASE / "logs", BASE / "run", BASE / "conf"
MYSQL_DATA, PMA_DIR, SSL_DIR, CONFIG_FILE = BASE / "mysql", BASE / "phpmyadmin", BASE / "ssl", BASE / "config.json"
MYSQL_MARK = CONF / "mysql.initialized"
UNIT_DIR = Path.home() / ".config/systemd/user"
DEFAULTS = {"apache_port": 8080, "ssl_port": 8443, "mysql_port": 3306, "tomcat_port": 8081, "lan": False,
            "editor": "", "browser": "", "autostart_apache": False, "autostart_mysql": False,
            "autostart_tomcat": False, "start_minimized": False}
STOP_TIMEOUT = {"apache": 10, "mysql": 60, "tomcat": 20}   # MySQL necesita tiempo para volcar a disco

PMA_SOURCES = ["/usr/share/phpmyadmin", "/usr/share/webapps/phpmyadmin"]
MODULE_DIRS = ["/usr/lib/apache2/modules", "/usr/lib/apache2", "/usr/lib64/httpd/modules"]
MIME_FILES = ["/etc/apache2/mime.types", "/etc/mime.types"]


class Problem(Exception):
    """Fallo previsto al preparar un módulo: se muestra en rojo en el log."""
    def __init__(self, *lines):
        super().__init__(lines[0])
        self.lines = lines


# ----------------------------------------------------------------- utilidades
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    BASE.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def first_existing(paths):
    return next((p for p in paths if os.path.exists(p)), None)


def which_any(*names):
    for n in names:
        p = shutil.which(n) or shutil.which(n, path="/usr/sbin:/sbin:/usr/local/sbin:/usr/bin")
        if p:
            return p
    return None


def run_out(cmd, timeout=4, stdout_only=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") if stdout_only else (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def listening_ports():
    """Puertos TCP en escucha: [{'port', 'pid', 'name', 'path'}]."""
    out = []
    for line in run_out(["ss", "-H", "-ltnp"], 3, True).splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        port = f[3].rsplit(":", 1)[-1]
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        name, pid, path = (m.group(1), m.group(2), "") if m else ("", "", "")
        if pid:
            try:
                path = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                pass
        out.append({"port": port, "pid": pid, "name": name, "path": path})
    return out


def child_pids(pid):
    try:
        return Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return []


def pid_valid(pid):
    """El PID existe y es un proceso nuestro (su línea de comandos apunta a nuestra carpeta)."""
    try:
        os.kill(pid, 0)
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return str(BASE) in cmd
    except (OSError, ValueError):
        return False


def read_pid(path):
    try:
        pid = int(Path(path).read_text().split()[0])
        return pid if pid_valid(pid) else None
    except Exception:
        return None


def os_name():
    try:
        for l in Path("/etc/os-release").read_text().splitlines():
            if l.startswith("PRETTY_NAME="):
                return l.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return "Linux"


def php_ini():
    return first_existing(sorted(glob.glob("/etc/php/*/apache2/php.ini")) + glob.glob("/etc/php*/apache2/php.ini"))


def free_port(start):
    for p in range(start, start + 100):
        if not port_open(p):
            return p
    return start


def unit_quote(a):
    return '"' + str(a).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


# --------------------------------------------------- detección de lo ya instalado
TOMCAT_HOMES = (sorted(glob.glob("/usr/share/tomcat*")) + sorted(glob.glob("/opt/tomcat*")) +
                sorted(glob.glob("/opt/apache-tomcat*")) + sorted(glob.glob("/usr/local/tomcat*")))
DETECT_SPEC = {
    "apache": dict(label="Apache", bins=("apache2", "httpd"), units=("apache2", "httpd")),
    "mysql": dict(label="MySQL", bins=("mysqld", "mariadbd"), units=("mysql", "mysqld", "mariadb")),
    "filezilla": dict(label="FileZilla (FTP)", bins=("vsftpd", "proftpd", "pure-ftpd"), units=("vsftpd", "proftpd", "pure-ftpd")),
    "mercury": dict(label="Mercury (mail)", bins=("postfix", "exim4", "sendmail"), units=("postfix", "exim4", "sendmail")),
    "tomcat": dict(label="Tomcat", bins=(), units=("tomcat10", "tomcat9", "tomcat", "tomcat11")),
}


def tomcat_home():
    return next((h for h in TOMCAT_HOMES if os.path.exists(f"{h}/bin/catalina.sh")), None)


def detect_system():
    """Qué hay ya en el sistema: binario, versión, servicio systemd (y su estado) y puertos usados."""
    units = {}
    for line in run_out(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"], 6, True).splitlines():
        f = line.split()
        if len(f) >= 2:
            units[f[0].removesuffix(".service")] = f[1]
    ports = listening_ports()
    result = {}
    for key, sp in DETECT_SPEC.items():
        info = dict(label=sp["label"], installed=False, binary=None, version="", unit=None,
                    unit_state="", active="", ports=[], notes=[], flavor="mysql")
        if key == "tomcat":
            home = tomcat_home()
            info["binary"] = f"{home}/bin/catalina.sh" if home else None
            info["home"] = home
        else:
            info["binary"] = which_any(*sp["bins"])
        unit = next((u for u in sp["units"] if u in units), None)
        info["unit"], info["unit_state"] = unit, units.get(unit, "")
        info["installed"] = bool(info["binary"] or unit)
        if unit:
            info["active"] = run_out(["systemctl", "is-active", unit], 4, True).strip()
        b = info["binary"] or ""
        if key == "apache" and b:
            m = re.search(r"Apache/([\d.]+)", run_out([b, "-v"]))
            info["version"] = m.group(1) if m else ""
            sites = [p for p in glob.glob("/etc/apache2/sites-enabled/*") if not p.endswith("000-default.conf")]
            if sites:
                info["notes"].append(f"{len(sites)} configured site(s) in /etc/apache2/sites-enabled")
        elif key == "mysql" and b:
            out = run_out([b, "--version"]).strip()
            m = re.search(r"Ver ([\d.]+)", out)
            info["flavor"] = "mariadb" if "MariaDB" in out else "mysql"
            info["version"] = ("MariaDB " if info["flavor"] == "mariadb" else "MySQL ") + (m.group(1) if m else "")
            if os.path.isdir("/var/lib/mysql"):
                info["notes"].append("system data directory /var/lib/mysql exists")
        elif key == "tomcat" and b:
            m = re.search(r"tomcat-?(\d+)", b)
            info["version"] = m.group(1) if m else ""
        for p in ports:
            nm = p["name"]
            if (key == "apache" and nm in ("apache2", "httpd")) or (key == "mysql" and nm in ("mysqld", "mariadbd")) or \
               (key == "tomcat" and nm == "java" and "tomcat" in run_out(["ps", "-o", "args=", "-p", p["pid"]]).lower()) or \
               (key == "filezilla" and nm in ("vsftpd", "proftpd", "pure-ftpd")) or (key == "mercury" and nm in ("master", "exim4", "sendmail")):
                info["ports"].append(p["port"])
        result[key] = info
    return result


def describe_detection(key, d):
    if not d["installed"]:
        return None
    parts = [f"{d['label']} detected" + (f" ({d['version']})" if d["version"] else "")]
    if d["binary"]:
        parts.append(f"at {d['binary']}")
    if d["unit"]:
        st = d["active"] if d["active"] in ("active", "inactive", "failed", "activating") else ""
        parts.append(f"system service '{d['unit']}'" + (f" is {st}" if st else "") + (f" ({d['unit_state']})" if d["unit_state"] else ""))
    if d["ports"]:
        parts.append("listening on port " + ", ".join(sorted(set(d["ports"]), key=int)))
    return " - ".join(parts)


# ------------------------------------------------------- generación de config
def prepare_dirs():
    for d in (HTDOCS, LOGS, RUN, CONF):
        d.mkdir(parents=True, exist_ok=True)
    index = HTDOCS / "index.php"
    if not index.exists():
        index.write_text(
            "<!doctype html><meta charset='utf-8'><title>Welcome to XAMPP</title>"
            "<body style='font-family:sans-serif;max-width:640px;margin:4rem auto'>"
            "<h1>Welcome to XAMPP for Linux</h1><p>Apache + PHP <?= PHP_VERSION ?> are running.</p>"
            "<p>Your files go in <code><?= htmlspecialchars(__DIR__) ?></code></p>"
            "<p><a href='/phpmyadmin/'>phpMyAdmin</a> · <a href='info.php'>phpinfo()</a></p></body>")
    info = HTDOCS / "info.php"
    if not info.exists():
        info.write_text("<?php phpinfo();\n")


def build_phpmyadmin():
    """Copia ligera de phpMyAdmin con config propia (root sin contraseña, como XAMPP)."""
    src = first_existing(PMA_SOURCES)
    if not src:
        return None
    if PMA_DIR.exists():
        shutil.rmtree(PMA_DIR)
    PMA_DIR.mkdir(parents=True)
    for item in Path(src).iterdir():
        if item.name == "config.inc.php":
            continue
        dst = PMA_DIR / item.name
        if item.name == "libraries":
            shutil.copytree(item.resolve(), dst, symlinks=True)
        elif item.is_dir():
            dst.symlink_to(item.resolve())
        else:
            shutil.copy2(item.resolve(), dst)
    vc = PMA_DIR / "libraries" / "vendor_config.php"
    if vc.exists():  # Debian/Ubuntu fijan la config en /etc/phpmyadmin: la redirigimos
        vc.write_text(re.sub(r"'configFile'\s*=>\s*[^,]+,", f"'configFile' => '{PMA_DIR / 'config.inc.php'}',", vc.read_text()))
    tmp = BASE / "pma-tmp"
    tmp.mkdir(exist_ok=True)
    (PMA_DIR / "config.inc.php").write_text(f"""<?php
$cfg['blowfish_secret'] = '{os.urandom(16).hex()}';
$cfg['TempDir'] = '{tmp}';
$i = 1;
$cfg['Servers'][$i]['auth_type'] = 'config';
$cfg['Servers'][$i]['user'] = 'root';
$cfg['Servers'][$i]['password'] = '';
$cfg['Servers'][$i]['host'] = 'localhost';
$cfg['Servers'][$i]['socket'] = '{RUN / "mysql.sock"}';
$cfg['Servers'][$i]['AllowNoPassword'] = true;
""")
    return PMA_DIR


def ensure_ssl():
    """Certificado autofirmado para https://localhost (como el de XAMPP). Devuelve (crt, key) o None."""
    crt, key = SSL_DIR / "server.crt", SSL_DIR / "server.key"
    if crt.exists() and key.exists():
        return crt, key
    if not shutil.which("openssl"):
        return None
    SSL_DIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-keyout", str(key), "-out", str(crt), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], capture_output=True)
    return (crt, key) if r.returncode == 0 else None


def build_apache_conf(cfg):
    """Genera httpd.conf. Devuelve (ruta, [avisos])."""
    mod_dir = next((d for d in MODULE_DIRS if glob.glob(f"{d}/mod_authz_core.so")), None)
    if not mod_dir:
        raise Problem("Apache modules not found. Is 'apache2' installed? (sudo apt install apache2)")
    warns = []
    php_mod = next(iter(sorted(glob.glob(f"{mod_dir}/*php*.so"))), None)
    if not php_mod:
        warns.append("PHP module not found (package libapache2-mod-php). Apache will run without PHP.")
    mime = first_existing(MIME_FILES)

    def load(ident, fname):
        return f'LoadModule {ident} "{mod_dir}/{fname}"' if os.path.exists(f"{mod_dir}/{fname}") else ""

    mods = [load(i, f) for i, f in (
        ("mpm_prefork_module", "mod_mpm_prefork.so"), ("unixd_module", "mod_unixd.so"),
        ("authz_core_module", "mod_authz_core.so"), ("authz_host_module", "mod_authz_host.so"),
        ("access_compat_module", "mod_access_compat.so"), ("log_config_module", "mod_log_config.so"),
        ("mime_module", "mod_mime.so"), ("dir_module", "mod_dir.so"), ("alias_module", "mod_alias.so"),
        ("autoindex_module", "mod_autoindex.so"), ("rewrite_module", "mod_rewrite.so"),
        ("headers_module", "mod_headers.so"), ("env_module", "mod_env.so"),
        ("setenvif_module", "mod_setenvif.so"))]
    if php_mod:
        mods.append(f'LoadModule php_module "{php_mod}"')
    bind = "0.0.0.0" if cfg["lan"] else "127.0.0.1"
    port, sport = cfg["apache_port"], cfg["ssl_port"]

    ssl_block, ssl_listen = "", ""
    ssl = ensure_ssl() if os.path.exists(f"{mod_dir}/mod_ssl.so") else None
    if ssl:
        mods += [load("socache_shmcb_module", "mod_socache_shmcb.so"), load("ssl_module", "mod_ssl.so")]
        cache = f'shmcb:{RUN}/ssl_scache(512000)' if os.path.exists(f"{mod_dir}/mod_socache_shmcb.so") else "none"
        ssl_listen = f"Listen {bind}:{sport} https"
        ssl_block = f"""
SSLSessionCache "{cache}"
SSLRandomSeed startup builtin
<VirtualHost *:{sport}>
    DocumentRoot "{HTDOCS}"
    SSLEngine on
    SSLCertificateFile "{ssl[0]}"
    SSLCertificateKeyFile "{ssl[1]}"
</VirtualHost>"""
    else:
        warns.append("HTTPS is disabled (needs mod_ssl and openssl).")

    pma = build_phpmyadmin()
    pma_block = f"""
Alias /phpmyadmin "{pma}"
<Directory "{pma}">
    Options FollowSymLinks
    DirectoryIndex index.php
    Require local
</Directory>""" if pma else ""
    if not pma:
        warns.append("phpMyAdmin not found (sudo apt install phpmyadmin).")
    conf = f"""ServerRoot "{BASE}"
ServerName localhost
Listen {bind}:{port}
{ssl_listen}
PidFile "{RUN / 'httpd.pid'}"
DefaultRuntimeDir "{RUN}"
Mutex "file:{RUN}" default
{chr(10).join(m for m in mods if m)}
{f'TypesConfig "{mime}"' if mime else ''}
ErrorLog "{LOGS / 'error.log'}"
LogFormat "%h %l %u %t \\"%r\\" %>s %b" common
CustomLog "{LOGS / 'access.log'}" common
DocumentRoot "{HTDOCS}"
<Directory />
    AllowOverride None
    Require all denied
</Directory>
<Directory "{HTDOCS}">
    Options Indexes FollowSymLinks
    AllowOverride All
    Require all granted
</Directory>
DirectoryIndex index.php index.html
<FilesMatch "\\.php$">
    SetHandler application/x-httpd-php
</FilesMatch>
<IfModule php_module>
    php_admin_value mysqli.default_socket "{RUN / 'mysql.sock'}"
    php_admin_value pdo_mysql.default_socket "{RUN / 'mysql.sock'}"
    php_admin_value mysqli.default_port "{cfg['mysql_port']}"
</IfModule>
{ssl_block}
{pma_block}
"""
    path = CONF / "httpd.conf"
    path.write_text(conf)
    return path, warns


def build_my_cnf(cfg, flavor):
    path = CONF / "my.cnf"
    # MySQL: mysqlx desactivado y sin import/export de archivos. MariaDB no entiende "NULL" (vacío ya significa sin restricción)
    extra = "mysqlx=OFF\nsecure-file-priv=NULL\n" if flavor == "mysql" else ""
    path.write_text(f"""[client]
socket="{RUN / 'mysql.sock'}"
port={cfg['mysql_port']}

[mysqld]
basedir=/usr
datadir="{MYSQL_DATA}"
socket="{RUN / 'mysql.sock'}"
port={cfg['mysql_port']}
bind-address=127.0.0.1
{extra}pid-file="{RUN / 'mysql.pid'}"
log-error="{LOGS / 'mysql_error.log'}"
tmpdir="{RUN}"
skip-name-resolve
""")
    return path


def build_tomcat_base(cfg, home):
    """CATALINA_BASE propio (sin root) copiado de la configuración que ya tengas instalada."""
    base = BASE / "tomcat"
    for d in ("webapps", "logs", "temp", "work", "conf"):
        (base / d).mkdir(parents=True, exist_ok=True)
    name = Path(home).name
    # Debian/Ubuntu: /etc/tomcatNN no es legible sin ser del grupo tomcat; sus plantillas están en <home>/etc
    cands = [Path(home) / "etc", Path(f"/etc/{name}"), Path(home) / "conf"]
    src = next((c for c in cands if os.access(c / "server.xml", os.R_OK)), None)
    if src is None:
        raise Problem("No readable Tomcat configuration (server.xml) found.")
    for item in src.iterdir():
        dst = base / "conf" / item.name
        if item.name == "server.xml" or dst.exists():
            continue
        try:
            shutil.copytree(item.resolve(), dst) if item.is_dir() else shutil.copy2(item.resolve(), dst)
        except PermissionError:
            pass
    xml = (src / "server.xml").read_text()
    xml = re.sub(r'port="8080"', f'port="{cfg["tomcat_port"]}"', xml)
    xml = re.sub(r'<Server port="\d+"', '<Server port="-1"', xml)   # sin puerto de apagado: se para con SIGTERM
    (base / "conf" / "server.xml").write_text(xml)
    web_src = next((p for p in (Path(f"/var/lib/{name}/webapps"), Path(home) / "webapps") if p.is_dir()), None)
    if web_src and not any((base / "webapps").iterdir()):
        for item in web_src.iterdir():
            dst = base / "webapps" / item.name
            shutil.copytree(item.resolve(), dst) if item.is_dir() else shutil.copy2(item.resolve(), dst)
    return base


# ------------------------------------------------------------------ interfaz
class VLabel(QWidget):
    """Texto vertical (el rótulo «Modules» del panel)."""
    def __init__(self, text):
        super().__init__()
        self.text = text
        self.setFixedWidth(22)

    def paintEvent(self, _):
        p = QPainter(self)
        f = QFont(self.font()); f.setBold(True); p.setFont(f)
        p.translate(0, self.height()); p.rotate(-90)
        p.drawText(QRect(0, 0, self.height(), self.width()), Qt.AlignmentFlag.AlignCenter, self.text)


class LogView(QTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setFont(QFont("DejaVu Sans", 9))
        self.document().setMaximumBlockCount(3000)
        self.setTabStopDistance(118)

    def add(self, tag, msg, error=False):
        t = datetime.now().strftime("%H:%M:%S")
        color = "#c00000" if error else "#000000"
        self.append(f'<span style="white-space:pre;color:{color}">{t}  {html.escape(f"[{tag}]")}\t{html.escape(msg)}</span>')


class Module:
    """Una fila del panel: Apache, MySQL, FileZilla, Mercury, Tomcat."""
    def __init__(self, key, label, port_key=None, available=True):
        self.key, self.label, self.port_key, self.available = key, label, port_key, available
        self.proc = QProcess()
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.state = "stopped"
        self.stopping = False
        self.init_proc = None
        self.wired = False
        self.tail = []

    def own(self):
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def initializing(self):
        return self.init_proc is not None and self.init_proc.state() != QProcess.ProcessState.NotRunning


PORTS = {"apache": (("apache_port", " (HTTP)"), ("ssl_port", " (HTTPS)")),
         "mysql": (("mysql_port", ""),), "tomcat": (("tomcat_port", ""),)}
PIDFILES = {"apache": RUN / "httpd.pid", "mysql": RUN / "mysql.pid"}
TAGS = {"apache": "Apache", "mysql": "mysql", "tomcat": "Tomcat"}
LOGFILES = {"apache": LOGS / "error.log", "mysql": LOGS / "mysql_error.log", "tomcat": LOGS / "tomcat.log"}


class SettingsDialog(QDialog):
    """Configuration of Control Panel."""
    def __init__(self, cfg, win):
        super().__init__(win)
        self.cfg, self.win = cfg, win
        self.setWindowTitle("Configuration of Control Panel")
        lay = QVBoxLayout(self)
        g = QGroupBox("Editor / Browser"); f = QFormLayout(g)
        self.editor = QLineEdit(cfg["editor"]); self.editor.setPlaceholderText("(default: system editor)")
        self.browser = QLineEdit(cfg["browser"]); self.browser.setPlaceholderText("(default: system browser)")
        f.addRow("Editor:", self.editor); f.addRow("Browser:", self.browser)
        lay.addWidget(g)
        g2 = QGroupBox("Autostart of modules"); h = QHBoxLayout(g2)
        self.auto = {}
        for k, n in (("apache", "Apache"), ("mysql", "MySQL"), ("filezilla", "FileZilla"), ("mercury", "Mercury"), ("tomcat", "Tomcat")):
            c = QCheckBox(n); c.setChecked(cfg.get(f"autostart_{k}", False))
            c.setEnabled(k in ("apache", "mysql") or (k == "tomcat" and win.mods["tomcat"].available)); self.auto[k] = c; h.addWidget(c)
        lay.addWidget(g2)
        self.minimized = QCheckBox("Start Control Panel Minimized"); self.minimized.setChecked(cfg["start_minimized"])
        lay.addWidget(self.minimized)
        self.linger_was = win.linger_enabled()
        self.linger = QCheckBox("Start installed services at boot, even before I log in (asks for admin password)")
        self.linger.setChecked(self.linger_was)
        lay.addWidget(self.linger)
        row = QHBoxLayout()
        ports = QPushButton("Service and Port Settings"); ports.clicked.connect(self.ports)
        save = QPushButton("Save"); save.clicked.connect(self.save)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        row.addWidget(ports); row.addStretch(); row.addWidget(save); row.addWidget(cancel)
        lay.addLayout(row)

    def ports(self):
        if any(self.win.is_running(k) for k in ("apache", "mysql", "tomcat")):
            self.win.log.add("main", "Stop the modules before changing the ports.", True); return
        d = QDialog(self); d.setWindowTitle("Service and Port Settings")
        f = QFormLayout(d)
        spins = {}
        for ck, label in (("apache_port", "Apache (Main) Port:"), ("ssl_port", "Apache (SSL) Port:"),
                          ("mysql_port", "MySQL Port:"), ("tomcat_port", "Tomcat HTTP Port:")):
            sp = QSpinBox(); sp.setRange(1024, 65535); sp.setValue(self.cfg[ck]); spins[ck] = sp; f.addRow(label, sp)
        lan = QCheckBox("Allow Apache to be reached from other devices on the network (LAN)")
        lan.setChecked(self.cfg["lan"]); f.addRow(lan)
        f.addRow(QLabel("<small>Without root only ports ≥ 1024 can be used. MySQL and phpMyAdmin always stay local.</small>"))
        ok = QPushButton("Save"); ok.clicked.connect(d.accept); f.addRow(ok)
        if d.exec():
            vals = {k: s.value() for k, s in spins.items()}
            if len(set(vals.values())) != len(vals):
                self.win.log.add("main", "Each module needs a different port.", True); return
            self.cfg.update(vals, lan=lan.isChecked())
            save_config(self.cfg); self.win.update_all_widgets()

    def save(self):
        self.cfg.update(editor=self.editor.text().strip(), browser=self.browser.text().strip(),
                        autostart_apache=self.auto["apache"].isChecked(),
                        autostart_mysql=self.auto["mysql"].isChecked(),
                        autostart_tomcat=self.auto["tomcat"].isChecked(),
                        start_minimized=self.minimized.isChecked())
        save_config(self.cfg)
        if self.linger.isChecked() != self.linger_was:
            self.win.set_linger(self.linger.isChecked())
        self.win.log.add("main", "Control Panel settings saved")
        self.accept()


class TableDialog(QDialog):
    def __init__(self, parent, title, headers, rows, size=(700, 400)):
        super().__init__(parent)
        self.setWindowTitle(title); self.resize(*size)
        lay = QVBoxLayout(self)
        t = QTableWidget(len(rows), len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().hide(); t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                t.setItem(r, c, QTableWidgetItem(str(v)))
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(t)
        b = QPushButton("Close"); b.clicked.connect(self.accept)
        lay.addWidget(b, alignment=Qt.AlignmentFlag.AlignRight)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITLE)
        self.cfg = load_config()
        prepare_dirs()
        self.detected = detect_system()
        self.mods = {
            "apache": Module("apache", "Apache", "apache_port"),
            "mysql": Module("mysql", "MySQL", "mysql_port"),
            "filezilla": Module("filezilla", "FileZilla", available=False),
            "mercury": Module("mercury", "Mercury", available=False),
            "tomcat": Module("tomcat", "Tomcat", "tomcat_port", available=bool(self.detected["tomcat"]["binary"])),
        }
        self.svc = {k: dict(installed=False, active=False, mainpid=None) for k in self.mods}
        self.widgets = {}
        self._async = []
        self._tick = 0
        self.refresh_services()

        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central); root.setContentsMargins(8, 6, 8, 8)
        top = QHBoxLayout()
        left = QVBoxLayout()
        head = QHBoxLayout()
        logo = QLabel()
        icon = self.icon_path()
        if icon:
            logo.setPixmap(QIcon(icon).pixmap(QSize(56, 56)))
        head.addWidget(logo)
        head.addWidget(QLabel(f"<span style='font-size:15pt'><b>XAMPP Control Panel v{VERSION}</b></span>"))
        head.addStretch()
        left.addLayout(head)
        left.addWidget(self.build_grid())
        top.addLayout(left, 1)
        side = QVBoxLayout(); side.setSpacing(4)
        for text, fn in (("Config", self.open_settings), ("Netstat", self.open_netstat), ("Shell", self.open_shell),
                         ("Explorer", self.open_explorer), ("Services", self.open_services),
                         ("Help", self.open_help), ("Quit", self.close)):
            b = QPushButton(text); b.setFixedWidth(82); b.clicked.connect(fn); side.addWidget(b)
        side.addStretch()
        top.addLayout(side)
        root.addLayout(top)
        self.log = LogView()
        self.log.setMinimumHeight(190)
        root.addWidget(self.log, 1)
        self.resize(720, 540)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable() and icon:
            self.tray = QSystemTrayIcon(QIcon(icon), self)
            menu = QMenu(); menu.addAction("Show Control Panel", self.showNormal); menu.addAction("Quit", self.close)
            self.tray.setContextMenu(menu); self.tray.setToolTip("XAMPP Control Panel"); self.tray.show()

        self.boot_log()
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(700)
        QTimer.singleShot(400, self.autostart)

    def icon_path(self):
        for p in (Path(__file__).with_name("xampp-linux.svg"), Path("/usr/share/icons/hicolor/scalable/apps/xampp-control-panel.svg")):
            if p.exists():
                return str(p)
        return None

    # ---- construcción del panel
    def build_grid(self):
        frame = QFrame(); frame.setFrameShape(QFrame.Shape.StyledPanel)
        g = QGridLayout(frame); g.setVerticalSpacing(4); g.setHorizontalSpacing(6)
        g.addWidget(VLabel("Modules"), 0, 0, 12, 1)
        for c, h in ((1, "Service"), (2, "Module"), (3, "PID(s)"), (4, "Port(s)")):
            l = QLabel(f"<b>{h}</b>"); l.setAlignment(Qt.AlignmentFlag.AlignCenter); g.addWidget(l, 0, c)
        a = QLabel("<b>Actions</b>"); a.setAlignment(Qt.AlignmentFlag.AlignCenter); g.addWidget(a, 0, 5, 1, 4)
        row = 1
        for key, m in self.mods.items():
            line = QFrame(); line.setFrameShape(QFrame.Shape.HLine); line.setFrameShadow(QFrame.Shadow.Sunken)
            g.addWidget(line, row, 1, 1, 8); row += 1
            svc = QPushButton("✖"); svc.setFlat(True); svc.setFixedWidth(34)
            svc.clicked.connect(lambda _=False, k=key: self.toggle_service(k))
            name = QLabel(m.label); name.setAlignment(Qt.AlignmentFlag.AlignCenter); name.setMinimumWidth(84)
            pid = QLabel(""); pid.setAlignment(Qt.AlignmentFlag.AlignCenter); pid.setMinimumWidth(52)
            port = QLabel(""); port.setAlignment(Qt.AlignmentFlag.AlignCenter); port.setMinimumWidth(70)
            btns = {n: QPushButton(n) for n in ("Start", "Admin", "Config", "Logs")}
            for i, b in enumerate(btns.values()):
                b.setFixedSize(62, 26); g.addWidget(b, row, 5 + i)
            for c, w in enumerate((svc, name, pid, port), start=1):
                g.addWidget(w, row, c)
            btns["Start"].clicked.connect(lambda _=False, k=key: self.toggle(k))
            btns["Admin"].clicked.connect(lambda _=False, k=key: self.admin(k))
            btns["Config"].clicked.connect(lambda _=False, k=key: self.config_menu(k))
            btns["Logs"].clicked.connect(lambda _=False, k=key: self.logs_menu(k))
            self.widgets[key] = dict(svc=svc, name=name, pid=pid, port=port, **btns)
            for b in btns.values():
                b.setEnabled(m.available)
            row += 1
        return frame

    def boot_log(self):
        L = self.log.add
        L("main", "Initializing Control Panel")
        L("main", f"Linux Version: {os_name()}")
        L("main", "XAMPP Version: 8.x (Apache + MySQL + PHP + phpMyAdmin)")
        L("main", f"Control Panel Version: {VERSION}  [ Compiled: {COMPILED} ]")
        if os.geteuid() == 0:
            L("main", "You are running with administrator rights! Modules will run as root.", True)
        else:
            L("main", "You are not running with administrator rights! This will work for the most part")
        L("main", f"XAMPP Installation Directory: \"{BASE}\"")
        L("main", "Checking for prerequisites")
        ok = True
        for name, exe in (("Apache", which_any("apache2", "httpd")), ("MySQL", which_any("mysqld", "mariadbd"))):
            if not exe:
                L("main", f"{name} was not found. Install it with: sudo apt install {'apache2' if name == 'Apache' else 'mysql-server'}", True); ok = False
        if not first_existing(PMA_SOURCES):
            L("main", "phpMyAdmin was not found. Install it with: sudo apt install phpmyadmin", True); ok = False
        if ok:
            L("main", "All prerequisites found")
        L("main", "Detecting existing installations")
        for key, d in self.detected.items():
            line = describe_detection(key, d)
            if line:
                L("main", line)
                for n in d["notes"]:
                    L("main", f"{d['label']}: {n}")
            elif key in ("apache", "mysql", "tomcat"):
                L("main", f"{d['label']}: no existing installation found")
        if self.detected["mysql"]["binary"] and self.detected["mysql"]["flavor"] == "mariadb":
            L("main", "MySQL is not available on this distribution: its drop-in replacement MariaDB will be used.")
        for key in ("apache", "mysql", "tomcat"):
            if self.svc[key]["installed"]:
                L("main", f"{self.mods[key].label} is installed as a service (systemd user service)")
            if self.is_running(key):
                L("main", f"{self.mods[key].label} is already running (PID {self.main_pid(key)}): started by a previous session or service")
        if self.cfg["lan"]:
            L("main", "LAN access is enabled: Apache listens on all network interfaces.")
        L("main", "Initializing Modules")
        L("main", "Starting Check-Timer")
        L("main", "Control Panel Ready")

    # ---- estado de los módulos
    def tag(self, key):
        return TAGS.get(key, key)

    def ext_pid(self, key):
        pf = PIDFILES.get(key)
        return read_pid(pf) if pf else None

    def main_pid(self, key):
        m = self.mods[key]
        if m.own():
            return m.proc.processId()
        return self.ext_pid(key) or (self.svc[key]["mainpid"] if self.svc[key]["active"] else None)

    def is_running(self, key):
        return self.mods[key].own() or self.ext_pid(key) is not None or self.svc[key]["active"]

    def pids(self, key):
        pid = self.main_pid(key)
        if not pid:
            return []
        return [str(pid)] + (child_pids(str(pid))[:1] if key == "apache" else [])

    def main_port(self, key):
        return self.cfg[self.mods[key].port_key]

    def refresh_services(self):
        for key in ("apache", "mysql", "tomcat"):
            s = self.svc[key]
            s["installed"] = (UNIT_DIR / f"xampp-{key}.service").exists()
            s["active"], s["mainpid"] = False, None
            if s["installed"]:
                unit = f"xampp-{key}.service"
                s["active"] = run_out(["systemctl", "--user", "is-active", unit], 2, True).strip() == "active"
                if s["active"]:
                    mp = run_out(["systemctl", "--user", "show", "-p", "MainPID", "--value", unit], 2, True).strip()
                    s["mainpid"] = int(mp) if mp.isdigit() and int(mp) > 0 else None

    def update_widgets(self, key):
        m, w = self.mods[key], self.widgets[key]
        running = self.is_running(key)
        w["name"].setStyleSheet("QLabel{background:#7CFC00;font-weight:bold;padding:3px 6px}" if m.state == "running"
                                else "QLabel{font-weight:bold;padding:3px 6px}")
        w["pid"].setText("\n".join(self.pids(key)))
        if m.port_key and running:
            w["port"].setText(", ".join(str(self.cfg[ck]) for ck, _ in PORTS[key]))
        else:
            w["port"].setText("")
        w["Start"].setText("Stop" if running or m.initializing() else "Start")
        w["Admin"].setEnabled(m.available and m.state == "running")
        inst = self.svc[key]["installed"]
        w["svc"].setText("✔" if inst else "✖")
        w["svc"].setStyleSheet(f"QPushButton{{color:{'#00a000' if inst else '#d00000'};font-weight:bold;border:none;background:transparent}}")
        det = self.detected[key]
        tip = "Installed as a service for your user (starts when you log in)" if inst else "Not installed as a service: click to install"
        if det["unit"]:
            tip += f"\nSystem service '{det['unit']}' also exists on this PC"
        w["svc"].setToolTip(tip)

    def update_all_widgets(self):
        for k in self.mods:
            self.update_widgets(k)

    def tick(self):
        self._tick += 1
        if self._tick % 3 == 0:
            self.refresh_services()
        for key, m in self.mods.items():
            if not m.port_key:
                continue
            new = "stopped"
            if self.is_running(key):
                new = "running" if port_open(self.main_port(key)) else "starting"
            if new != m.state:
                if new in ("running", "stopped"):
                    self.log.add(self.tag(key), f"Status change detected: {new}")
                m.state = new
            self.update_widgets(key)

    # ---- diagnóstico (lo que Windows deja en blanco)
    def diagnose(self, key):
        lines, hints = [], []
        try:
            allv = [l.strip() for l in LOGFILES[key].read_text(errors="replace").splitlines() if l.strip()]
            bad = [l for l in allv if re.search(r"error|fatal|crit|denied|can't|cannot|failed|unable|\[warning\]|emerg|alert", l, re.I)]
            lines = (bad or allv)[-3:]
        except Exception:
            pass
        if not lines:   # el módulo murió antes de poder abrir su log: usamos lo que escribió en pantalla
            lines = [l.strip() for l in self.mods[key].tail][-3:]
        text = "\n".join(lines).lower()
        if "address already in use" in text or "could not bind" in text or "make_sock" in text:
            hints.append("Another program is using the port. Use Config > Service and Port Settings to pick another one.")
        if "permission denied" in text or "errno: 13" in text or "can't create/write" in text:
            hints.append("Permission problem. On Ubuntu this is often AppArmor: run  sudo dmesg | grep -i apparmor")
        if "innodb" in text and ("corrupt" in text or "crash" in text or "recovery" in text):
            hints.append("MySQL is recovering after an unclean shutdown. Wait a minute and press Start again.")
        if "lock" in text and "mysql" in key:
            hints.append("Another MySQL is using this data directory. Stop it first.")
        return lines, hints

    def report_failure(self, key, headline):
        self.err(key, headline)
        lines, hints = self.diagnose(key)
        for l in lines:
            self.log.add(self.tag(key), "  " + l[:220], True)
        for h in hints:
            self.log.add(self.tag(key), "Hint: " + h, True)
        self.log.add(self.tag(key), "Use the Logs button to see the full error log.", True)

    def err(self, key, *msgs):
        self.log.add(self.tag(key), "Problem detected!", True)
        for msg in msgs:
            self.log.add(self.tag(key), msg, True)

    # ---- preparación de cada módulo
    def prepare(self, key):
        """Genera la configuración y devuelve {'exe','args','env'} o lanza Problem."""
        if key == "apache":
            exe = which_any("apache2", "httpd")
            if not exe:
                raise Problem("Apache is not installed. Install it with: sudo apt install apache2")
            if (RUN / "httpd.pid").exists() and self.ext_pid("apache") is None:
                (RUN / "httpd.pid").unlink(missing_ok=True)
            conf, warns = build_apache_conf(self.cfg)
            for w in warns:
                self.log.add("Apache", w, True)
            r = subprocess.run([exe, "-t", "-f", str(conf)], capture_output=True, text=True)
            if r.returncode != 0:
                raise Problem("Apache configuration test failed:", *[l for l in (r.stderr or r.stdout).strip().splitlines()[-4:]])
            return dict(exe=exe, args=["-f", str(conf), "-D", "FOREGROUND"], env={})
        if key == "mysql":
            exe = which_any("mysqld", "mariadbd")
            if not exe:
                raise Problem("MySQL is not installed. Install it with: sudo apt install mysql-server")
            if self.ext_pid("mysql") is None and not self.mods["mysql"].own():   # restos de un apagado brusco
                for f in ("mysql.pid", "mysql.sock", "mysql.sock.lock", "mysqlx.sock", "mysqlx.sock.lock"):
                    (RUN / f).unlink(missing_ok=True)
            cnf = build_my_cnf(self.cfg, self.detected["mysql"]["flavor"])
            return dict(exe=exe, args=[f"--defaults-file={cnf}"], env={})
        if key == "tomcat":
            home = self.detected["tomcat"].get("home")
            if not home:
                raise Problem("Tomcat was not found on this system.")
            if not shutil.which("java"):
                raise Problem("Java was not found. Install it with: sudo apt install default-jre-headless")
            base = build_tomcat_base(self.cfg, home)
            return dict(exe=f"{home}/bin/catalina.sh", args=["run"],
                        env={"CATALINA_HOME": home, "CATALINA_BASE": str(base), "CATALINA_TMPDIR": str(base / "temp")})
        raise Problem(f"{key} cannot be started on Linux.")

    def mysql_needs_init(self):
        if MYSQL_MARK.exists():
            return False
        if (MYSQL_DATA / "mysql.ibd").exists() or (MYSQL_DATA / "mysql").is_dir():   # datos de una versión anterior
            MYSQL_MARK.write_text("adopted\n")
            return False
        return True

    def init_mysql(self, then):
        key, m = "mysql", self.mods["mysql"]
        self.log.add("mysql", "First run: initializing the data directory (this takes a few seconds)...")
        if MYSQL_DATA.exists():
            shutil.rmtree(MYSQL_DATA, ignore_errors=True)
        MYSQL_DATA.mkdir(parents=True)
        cnf = CONF / "my.cnf"
        if self.detected["mysql"]["flavor"] == "mariadb":
            installer = which_any("mariadb-install-db", "mysql_install_db")
            cmd = [installer or "mariadb-install-db", "--no-defaults", f"--datadir={MYSQL_DATA}", "--basedir=/usr",
                   "--auth-root-authentication-method=normal", "--skip-test-db"]
        else:
            cmd = [which_any("mysqld") or "mysqld", f"--defaults-file={cnf}", "--initialize-insecure"]
        ip = QProcess(); m.init_proc = ip
        ip.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        def finished(code, _s):
            if code != 0:
                self.report_failure(key, "Could not initialize the MySQL data directory.")
                return
            MYSQL_MARK.write_text("ok\n")
            self.log.add("mysql", "Data directory ready (user root, no password)")
            then()
        ip.finished.connect(finished)
        ip.start(cmd[0], cmd[1:])

    # ---- arranque / parada
    def toggle(self, key):
        if self.is_running(key) or self.mods[key].initializing():
            self.stop(key)
        else:
            self.start(key)

    def start(self, key):
        m = self.mods[key]
        self.log.add(self.tag(key), f"Attempting to start {m.label} app...")
        try:
            spec = self.prepare(key)
        except Problem as p:
            self.err(key, *p.lines); return
        if not self.check_ports(key):
            return
        if key == "mysql" and self.mysql_needs_init():
            self.init_mysql(lambda: self.launch(key, spec)); return
        self.launch(key, spec)

    def launch(self, key, spec):
        m = self.mods[key]
        if self.svc[key]["installed"]:
            unit = f"xampp-{key}.service"
            def done(code, out):
                if code != 0:
                    self.report_failure(key, f"systemctl could not start the {m.label} service.")
                    for l in run_out(["journalctl", "--user", "-u", unit, "-n", "4", "--no-pager"], 4, True).splitlines()[-4:]:
                        self.log.add(self.tag(key), "  " + l[:220], True)
                else:
                    QTimer.singleShot(6000, lambda: None if self.is_running(key) else self.report_failure(key, f"{m.label} stopped right after starting."))
            self.async_run(["systemctl", "--user", "start", unit], done)
            return
        self.wire(key)
        env = QProcessEnvironment.systemEnvironment()
        for k, v in spec["env"].items():
            env.insert(k, v)
        m.proc.setProcessEnvironment(env)
        exe, args = spec["exe"], spec["args"]
        if key == "apache" and shutil.which("setsid"):
            # Apache, al parar, envía SIGTERM a todo su grupo de procesos: lo aislamos para que no se lleve al panel.
            exe, args = "setsid", [exe] + args
        m.proc.start(exe, args)

    def wire(self, key):
        m = self.mods[key]
        if m.wired:
            return
        m.wired = True
        def out():
            txt = bytes(m.proc.readAllStandardOutput()).decode(errors="replace")
            if key == "tomcat":   # Tomcat es muy verboso: todo al archivo, al panel solo lo importante
                with open(LOGS / "tomcat.log", "a") as f:
                    f.write(txt)
                txt = "\n".join(l for l in txt.splitlines() if re.search(r"SEVERE|Server startup|Exception|ERROR", l))
            if txt.strip():
                m.tail = (m.tail + [l for l in txt.splitlines() if l.strip()])[-6:]
                self.log.add(self.tag(key), txt.strip())
        m.proc.readyReadStandardOutput.connect(out)
        def done(code, status):
            crashed = code != 0 or status != QProcess.ExitStatus.NormalExit
            was_stopping, m.stopping = m.stopping, False
            if crashed and not was_stopping:
                self.report_failure(key, f"Error: {m.label} shutdown unexpectedly.")
                self.log.add(self.tag(key), "This may be due to a blocked port, missing dependencies, improper privileges or a crash.", True)
        m.proc.finished.connect(done)

    def async_run(self, cmd, done):
        p = QProcess(); self._async.append(p)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        def fin(code, _s):
            out = bytes(p.readAllStandardOutput()).decode(errors="replace")
            self._async.remove(p)
            done(code, out)
        p.finished.connect(fin)
        p.start(cmd[0], cmd[1:])

    def stop(self, key):
        m = self.mods[key]
        if m.initializing():
            self.log.add("mysql", "Initialization cancelled.", True)
            m.init_proc.kill(); return
        self.log.add(self.tag(key), f"Attempting to stop {m.label} app...")
        limit = STOP_TIMEOUT[key] * 1000
        if self.svc[key]["installed"] and (self.svc[key]["active"] or not m.own()):
            self.async_run(["systemctl", "--user", "stop", f"xampp-{key}.service"],
                           lambda code, out: self.log.add(self.tag(key), f"systemctl stop failed: {out.strip()}", True) if code else None)
            return
        if m.own():
            m.stopping = True
            m.proc.terminate()
            QTimer.singleShot(limit, lambda: m.proc.kill() if m.own() else None)
            return
        pid = self.ext_pid(key)    # proceso de una sesión anterior (el panel se cerró sin pararlo)
        if pid:
            os.kill(pid, signal.SIGTERM)
            def force():
                if pid_valid(pid):
                    os.kill(pid, signal.SIGKILL)
            QTimer.singleShot(limit, force)

    # ---- puertos ocupados / servicios del sistema
    def unit_for(self, who):
        if not who:
            return None
        key = {"apache2": "apache", "httpd": "apache", "mysqld": "mysql", "mariadbd": "mysql", "java": "tomcat"}.get(who["name"])
        return self.detected[key]["unit"] if key and self.detected[key]["active"] == "active" else None

    def ask_conflict(self, key, port, who, unit):
        by = f' by "{who["name"]}" (PID {who["pid"]})' if who and who["pid"] else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(f"{self.mods[key].label} - port in use")
        box.setText(f"Port {port} is already in use{by}.")
        box.setInformativeText("Another program (maybe an existing installation on this PC) is using it. What do you want to do?")
        b_port = box.addButton(f"Use a free port ({free_port(port + 1)})", QMessageBox.ButtonRole.AcceptRole)
        b_stop = box.addButton(f"Stop system service '{unit}'", QMessageBox.ButtonRole.DestructiveRole) if unit else None
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        c = box.clickedButton()
        return "port" if c is b_port else ("stop" if b_stop is not None and c is b_stop else None)

    def check_ports(self, key):
        m = self.mods[key]
        for ck, desc in PORTS[key]:
            port = self.cfg[ck]
            if not port_open(port):
                continue
            who = next((p for p in listening_ports() if p["port"] == str(port)), None)
            by = f' by "{who["path"] or who["name"]}" with PID {who["pid"]}' if who and who["pid"] else ""
            self.err(key, f"{m.label} WILL NOT start without the configured ports free!", f"Port {port}{desc} in use{by}")
            unit = self.unit_for(who)
            choice = self.ask_conflict(key, port, who, unit)
            if choice == "port":
                new = free_port(port + 1)
                self.cfg[ck] = new
                save_config(self.cfg); self.update_all_widgets()
                self.log.add(self.tag(key), f"Port changed to {new}. Starting again...")
                QTimer.singleShot(0, lambda: self.toggle(key))
            elif choice == "stop":
                self.log.add("main", f"Stopping system service '{unit}' (administrator password required)...")
                def done(code, out):
                    if code == 0:
                        self.log.add("main", f"System service '{unit}' stopped.")
                        self.detected = detect_system()
                        QTimer.singleShot(300, lambda: self.toggle(key))
                    else:
                        self.log.add("main", f"Could not stop '{unit}'. Stop it manually: sudo systemctl stop {unit}", True)
                self.async_run(["pkexec", "systemctl", "stop", unit], done)
            return False
        return True

    # ---- servicios (el ✖/✔ de la columna Service)
    def ask_yes_no(self, text):
        return QMessageBox.question(self, "XAMPP Control Panel", text) == QMessageBox.StandardButton.Yes

    def user_systemd_ok(self):
        try:
            return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=4).returncode == 0
        except Exception:
            return False

    def toggle_service(self, key):
        m = self.mods[key]
        if key not in ("apache", "mysql", "tomcat") or not m.available:
            self.show_detection(key); return
        if not self.user_systemd_ok():
            self.log.add("main", "systemd user services are not available on this system.", True); return
        if self.svc[key]["installed"]:
            if self.ask_yes_no(f"Do you want to uninstall the {m.label} service?"):
                self.uninstall_service(key)
        elif self.ask_yes_no(f"Do you want to install the {m.label} service?\nIt will start automatically when you log in "
                             "(or at boot if you enable that in Config)."):
            self.install_service(key)

    def install_service(self, key):
        m = self.mods[key]
        try:
            spec = self.prepare(key)
        except Problem as p:
            self.err(key, *p.lines); return
        if key == "mysql" and self.mysql_needs_init():
            self.init_mysql(lambda: self.install_service(key)); return
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        exe = spec["exe"] if " " not in spec["exe"] else unit_quote(spec["exe"])
        lines = ["[Unit]", f"Description=XAMPP Control Panel - {m.label}", "After=network.target", "",
                 "[Service]", "Type=simple",
                 "ExecStart=" + " ".join([exe] + [unit_quote(a) for a in spec["args"]])]
        lines += [f"Environment={unit_quote(f'{k}={v}')}" for k, v in spec["env"].items()]
        lines += [f"TimeoutStopSec={STOP_TIMEOUT[key]}", "", "[Install]", "WantedBy=default.target", ""]
        (UNIT_DIR / f"xampp-{key}.service").write_text("\n".join(lines))
        run_out(["systemctl", "--user", "daemon-reload"])
        out = run_out(["systemctl", "--user", "enable", f"xampp-{key}.service"])
        self.refresh_services(); self.update_widgets(key)
        self.log.add(self.tag(key), f"Service installed: {m.label} will start when you log in.")

    def uninstall_service(self, key):
        unit = f"xampp-{key}.service"
        run_out(["systemctl", "--user", "disable", "--now", unit], 30)
        (UNIT_DIR / unit).unlink(missing_ok=True)
        run_out(["systemctl", "--user", "daemon-reload"])
        self.refresh_services(); self.update_widgets(key)
        self.log.add(self.tag(key), "Service uninstalled.")

    def linger_enabled(self):
        return run_out(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"], 3, True).strip() == "yes"

    def set_linger(self, on):
        user = os.environ.get("USER", "")
        self.log.add("main", "Changing boot-time startup (administrator password required)...")
        self.async_run(["pkexec", "loginctl", "enable-linger" if on else "disable-linger", user],
                       lambda code, out: self.log.add("main", "Boot-time startup " + ("enabled." if on else "disabled.") if code == 0
                                                      else "Could not change boot-time startup.", code != 0))

    def autostart(self):
        if self.cfg["start_minimized"]:
            self.showMinimized()
        for key in ("apache", "mysql", "tomcat"):
            if self.cfg[f"autostart_{key}"] and self.mods[key].available and not self.is_running(key):
                self.toggle(key)

    # ---- acciones por módulo
    def show_detection(self, key):
        d = self.detected[key]
        self.log.add("main", describe_detection(key, d) or f"{d['label']}: no existing installation found")

    def open_url(self, url):
        b = self.cfg["browser"]
        if b:
            subprocess.Popen([b, url])
        else:
            QDesktopServices.openUrl(QUrl(url))

    def open_file(self, path):
        path = str(path)
        if not os.path.exists(path):
            self.log.add("main", f"File not found: {path}", True); return
        ed = self.cfg["editor"]
        if ed and os.path.isfile(path):
            subprocess.Popen([ed, path])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def admin(self, key):
        if key == "apache":
            self.open_url(f"http://localhost:{self.cfg['apache_port']}/")
        elif key == "tomcat":
            self.open_url(f"http://localhost:{self.cfg['tomcat_port']}/")
        elif key == "mysql":
            if self.mods["apache"].state != "running":
                self.log.add("mysql", "phpMyAdmin needs Apache running: start Apache first.", True); return
            self.open_url(f"http://localhost:{self.cfg['apache_port']}/phpmyadmin/")

    def popup(self, key, items):
        menu = QMenu(self)
        for text, fn in items:
            menu.addAction(text, fn)
        btn = self.widgets[key]["Config"]
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def config_menu(self, key):
        if key == "apache":
            def conf():
                try:
                    build_apache_conf(self.cfg)
                except Problem as p:
                    self.err("apache", *p.lines); return
                self.open_file(CONF / "httpd.conf")
            self.popup(key, [("Apache (httpd.conf)", conf),
                             ("PHP (php.ini)", lambda: self.open_file(php_ini() or "/etc/php/php.ini")),
                             ("phpMyAdmin (config.inc.php)", lambda: self.open_file(PMA_DIR / "config.inc.php")),
                             ("<Browse> [Apache]", lambda: self.open_file(BASE))])
        elif key == "mysql":
            def cnf():
                build_my_cnf(self.cfg, self.detected["mysql"]["flavor"]); self.open_file(CONF / "my.cnf")
            self.popup(key, [("my.cnf", cnf), ("<Browse> [MySQL]", lambda: self.open_file(BASE))])
        elif key == "tomcat":
            c = BASE / "tomcat" / "conf"
            self.popup(key, [("Tomcat (server.xml)", lambda: self.open_file(c / "server.xml")),
                             ("Tomcat (tomcat-users.xml)", lambda: self.open_file(c / "tomcat-users.xml")),
                             ("<Browse> [Tomcat]", lambda: self.open_file(BASE / "tomcat"))])

    def logs_menu(self, key):
        if key == "apache":
            self.popup(key, [("Apache (error.log)", lambda: self.open_file(LOGS / "error.log")),
                             ("Apache (access.log)", lambda: self.open_file(LOGS / "access.log")),
                             ("<Browse> [Apache]", lambda: self.open_file(LOGS))])
        elif key == "mysql":
            self.popup(key, [("MySQL (mysql_error.log)", lambda: self.open_file(LOGS / "mysql_error.log")),
                             ("<Browse> [MySQL]", lambda: self.open_file(LOGS))])
        elif key == "tomcat":
            self.popup(key, [("Tomcat (tomcat.log)", lambda: self.open_file(LOGS / "tomcat.log")),
                             ("<Browse> [Tomcat]", lambda: self.open_file(BASE / "tomcat" / "logs"))])

    # ---- botones laterales
    def open_settings(self):
        SettingsDialog(self.cfg, self).exec()

    def open_netstat(self):
        rows = [(p["pid"], p["port"], p["name"], p["path"]) for p in listening_ports()]
        TableDialog(self, "Netstat", ["PID", "Port(s)", "Name", "Path"], rows).exec()

    def open_shell(self):
        for cmd in (["gnome-terminal", "--working-directory", str(BASE)], ["konsole", "--workdir", str(BASE)],
                    ["xfce4-terminal", "--working-directory", str(BASE)], ["mate-terminal", "--working-directory", str(BASE)],
                    ["x-terminal-emulator"], ["xterm"]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, cwd=str(BASE)); return
        self.log.add("main", "No terminal emulator found.", True)

    def open_explorer(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(BASE)))

    def open_services(self):
        rows = []
        out = run_out(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager"], 6, True)
        for line in out.splitlines():
            f = line.split(None, 4)
            if len(f) >= 4:
                rows.append((f[0], f[4] if len(f) > 4 else "", f"{f[2]} ({f[3]})"))
        TableDialog(self, "Services", ["Name", "Description", "Status"], rows, (820, 460)).exec()

    def open_help(self):
        QDesktopServices.openUrl(QUrl("https://www.apachefriends.org/"))

    # ---- salir
    def ask_quit(self):
        return QMessageBox.question(self, "XAMPP Control Panel",
                                    "There are still some modules running. Do you really want to quit?\n"
                                    "Modules started by this panel will be stopped (services stay running).") == QMessageBox.StandardButton.Yes

    def closeEvent(self, e):
        own = [k for k, m in self.mods.items() if m.own() or m.initializing()]
        if own and not self.ask_quit():
            e.ignore(); return
        for k in own:
            m = self.mods[k]
            if m.initializing():
                m.init_proc.kill(); m.init_proc.waitForFinished(3000)
            if m.own():
                m.stopping = True
                m.proc.terminate()
                if not m.proc.waitForFinished(STOP_TIMEOUT[k] * 1000):
                    m.proc.kill(); m.proc.waitForFinished(3000)
        e.accept()


STYLE = """
QMainWindow, QDialog, QWidget#central { background: #f0f0f0; }
QPushButton { background: #e1e1e1; border: 1px solid #adadad; padding: 2px 6px; }
QPushButton:hover { background: #e5f1fb; border: 1px solid #0078d7; }
QPushButton:pressed { background: #cce4f7; border: 1px solid #005499; }
QPushButton:disabled { color: #a0a0a0; background: #cccccc; border: 1px solid #bfbfbf; }
QTextEdit { background: #ffffff; border: 1px solid #828790; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setApplicationName("XAMPP Control Panel")
    app.setDesktopFileName("xampp-control-panel")
    w = MainWindow()
    icon = w.icon_path()
    if icon:
        app.setWindowIcon(QIcon(icon))
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
