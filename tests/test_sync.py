import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILL_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = SKILL_ROOT / "sync_herdr_watch.py"

def load_installer():
    if not INSTALLER_PATH.is_file():
        raise AssertionError("sync_herdr_watch.py has not been implemented")
    spec = importlib.util.spec_from_file_location("sync_herdr_watch", INSTALLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_source(root, scripts=None, references=None):
    scripts = scripts or {"watch.py": "print('watch')\n"}
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("# Fixture skill\n", encoding="utf-8")
    (root / "scripts").mkdir()
    for name, contents in scripts.items():
        (root / "scripts" / name).write_text(contents, encoding="utf-8")
    if references is not None:
        (root / "references").mkdir()
        for name, contents in references.items():
            (root / "references" / name).write_text(contents, encoding="utf-8")


class InstallTests(unittest.TestCase):
    def test_install_copies_runtime_files_and_preserves_unrelated_files(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "profile" / "skills"
            create_source(
                source,
                scripts={"entry.py": "print('entry')\n", "helper.py": "VALUE = 1\n"},
                references={"usage.md": "usage\n", "ignore.txt": "ignore\n"},
            )
            unrelated = skills_dir / "other-skill" / "keep.txt"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("keep\n", encoding="utf-8")
            old_file = skills_dir / "herdr-watch" / "local.txt"
            old_file.parent.mkdir(parents=True)
            old_file.write_text("keep local\n", encoding="utf-8")
            installed = installer.install(source, [skills_dir])
            target = skills_dir / "herdr-watch"
            self.assertEqual([target.resolve()], installed)
            self.assertEqual("# Fixture skill\n", (target / "SKILL.md").read_text())
            self.assertEqual("VALUE = 1\n", (target / "scripts" / "helper.py").read_text())
            self.assertEqual("usage\n", (target / "references" / "usage.md").read_text())
            self.assertFalse((target / "references" / "ignore.txt").exists())
            self.assertEqual("keep\n", unrelated.read_text())
            self.assertEqual("keep local\n", old_file.read_text())

    def test_install_accepts_missing_references_directory(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "skills"
            create_source(source)
            installer.install(source, [skills_dir])
            self.assertTrue((skills_dir / "herdr-watch" / "scripts" / "watch.py").is_file())
            self.assertFalse((skills_dir / "herdr-watch" / "references").exists())

    def test_install_deduplicates_destinations_by_realpath(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            real_skills = base / "real" / "skills"
            alias_skills = base / "alias-skills"
            create_source(source)
            real_skills.mkdir(parents=True)
            alias_skills.symlink_to(real_skills, target_is_directory=True)
            installed = installer.install(source, [real_skills, alias_skills])
            self.assertEqual([real_skills.resolve() / "herdr-watch"], installed)

    def test_invalid_python_aborts_before_creating_any_destination(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            first = base / "first" / "skills"
            second = base / "second" / "skills"
            create_source(source, scripts={"broken.py": "if True print('broken')\n"})
            with self.assertRaises(SyntaxError):
                installer.install(source, [first, second])
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_managed_file_symlink_does_not_overwrite_external_target(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "skills"
            target = skills_dir / "herdr-watch"
            external = base / "external.md"
            create_source(source)
            target.mkdir(parents=True)
            external.write_text("external\n", encoding="utf-8")
            (target / "SKILL.md").symlink_to(external)
            installer.install(source, [skills_dir])
            self.assertEqual("external\n", external.read_text())
            self.assertFalse((target / "SKILL.md").is_symlink())
            self.assertEqual("# Fixture skill\n", (target / "SKILL.md").read_text())

    def test_install_copies_the_python_bytes_that_were_validated(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "skills"
            create_source(source, scripts={"watch.py": "VALUE = 1\n"})
            validate = installer._validate_python

            def mutate_after_validation(scripts):
                validate(scripts)
                (source / "scripts" / "watch.py").write_text(
                    "if True print('changed')\n", encoding="utf-8"
                )
            installer._validate_python = mutate_after_validation
            installer.install(source, [skills_dir])
            installed = skills_dir / "herdr-watch" / "scripts" / "watch.py"
            self.assertEqual("VALUE = 1\n", installed.read_text())
            compile(installed.read_bytes(), str(installed), "exec")

    def test_destination_collision_leaves_existing_installation_unchanged(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "skills"
            target = skills_dir / "herdr-watch"
            create_source(source)
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("old skill\n", encoding="utf-8")
            (target / "scripts").write_text("collision\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                installer.install(source, [skills_dir])
            self.assertEqual("old skill\n", (target / "SKILL.md").read_text())
            self.assertEqual("collision\n", (target / "scripts").read_text())

    def test_unrelated_change_during_install_is_preserved(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "skills"
            unrelated = skills_dir / "herdr-watch" / "notes.txt"
            create_source(source)
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("before\n", encoding="utf-8")
            stage_target = installer._stage_target
            def change_after_staging(source_files, destination):
                staged = stage_target(source_files, destination)
                unrelated.write_text("during\n", encoding="utf-8")
                return staged
            installer._stage_target = change_after_staging
            installer.install(source, [skills_dir])
            self.assertEqual("during\n", unrelated.read_text())

    def test_keyboard_interrupt_restores_existing_installation(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "skills"
            old_skill = skills_dir / "herdr-watch" / "SKILL.md"
            create_source(source)
            old_skill.parent.mkdir(parents=True)
            old_skill.write_text("old skill\n", encoding="utf-8")
            replace = installer.os.replace
            def interrupt_second_replace(source_name, destination_name):
                if Path(source_name).name.startswith(".herdr-watch-stage-"):
                    raise KeyboardInterrupt()
                replace(source_name, destination_name)

            installer.os.replace = interrupt_second_replace
            with self.assertRaises(KeyboardInterrupt):
                installer.install(source, [skills_dir])
            installer.os.replace = replace

            self.assertTrue(old_skill.is_file())
            self.assertEqual("old skill\n", old_skill.read_text())

    def test_install_works_without_posix_fchmod(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            skills_dir = base / "skills"
            create_source(source)
            fchmod = getattr(installer.os, "fchmod", None)
            if fchmod is not None:
                delattr(installer.os, "fchmod")
            try:
                installer.install(source, [skills_dir])
            finally:
                if fchmod is not None:
                    installer.os.fchmod = fchmod
            installed = skills_dir / "herdr-watch" / "scripts" / "watch.py"
            self.assertEqual("print('watch')\n", installed.read_text())


class DestinationTests(unittest.TestCase):
    def test_default_destinations_only_create_primary_roots_when_optional_absent(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            destinations = installer.default_destinations({}, home)
            self.assertEqual(
                [
                    home / ".claude" / "skills",
                    home / ".codex" / "skills",
                ],
                destinations,
            )

    def test_default_destinations_include_existing_optional_profiles_and_overrides(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for rel in (".agents", ".zcode", ".kimicode", ".config/muse", ".gemini/config", ".pi/agent"):
                (home / rel).mkdir(parents=True)
            destinations = installer.default_destinations({}, home)
            self.assertEqual(
                [
                    home / ".claude" / "skills",
                    home / ".codex" / "skills",
                    home / ".agents" / "skills",
                    home / ".zcode" / "skills",
                    home / ".kimicode" / "skills",
                    home / ".config" / "muse" / "skills",
                    home / ".gemini" / "config" / "skills",
                    home / ".pi" / "agent" / "skills",
                ],
                destinations,
            )
            environment = {
                "CLAUDE_CONFIG_DIR": "/config/claude",
                "ZCODE_CONFIG_DIR": "/config/zcode",
                "KIMICODE_CONFIG_DIR": "/config/kimi",
                "CODEX_HOME": "/config/codex",
                "XDG_CONFIG_HOME": str(home / ".config"),
            }
            overridden = installer.default_destinations(environment, home)
            self.assertEqual(
                [
                    Path("/config/claude/skills"),
                    Path("/config/codex/skills"),
                    Path("/config/zcode/skills"),
                    Path("/config/kimi/skills"),
                    home / ".config" / "muse" / "skills",
                    home / ".gemini" / "config" / "skills",
                    home / ".pi" / "agent" / "skills",
                ],
                overridden,
            )


class CommandLineTests(unittest.TestCase):
    def test_repeated_destination_installs_only_to_requested_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "first"
            second = base / "second"
            fake_home = base / "home"
            environment = os.environ.copy()
            environment["HOME"] = str(fake_home)
            environment.pop("CODEX_HOME", None)
            command = [sys.executable, str(INSTALLER_PATH)]
            command.extend(["--destination", str(first), "--destination", str(second)])
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((first / "herdr-watch" / "SKILL.md").is_file())
            self.assertTrue((second / "herdr-watch" / "SKILL.md").is_file())
            self.assertFalse((fake_home / ".agents").exists())

    def test_shell_wrapper_forwards_arguments_to_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            skills_dir = base / "skills"
            environment = os.environ.copy()
            environment["HOME"] = str(base / "home")
            environment["CLAUDE_CONFIG_DIR"] = str(base / "claude")
            environment["ZCODE_CONFIG_DIR"] = str(base / "zcode")
            environment["KIMICODE_CONFIG_DIR"] = str(base / "kimi")
            environment["CODEX_HOME"] = str(base / "codex")
            result = subprocess.run(
                [str(SKILL_ROOT / "sync-herdr-watch.sh"), "--destination", str(skills_dir)],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((skills_dir / "herdr-watch" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
