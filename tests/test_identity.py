import unittest

from agent_box.identity import HostIdentity, _sanitize_username, detect_host_identity


class SanitizeUsernameTests(unittest.TestCase):
    def test_normal_username_preserved(self):
        self.assertEqual(_sanitize_username("alex", 1000), "alex")

    def test_uppercase_lowered(self):
        self.assertEqual(_sanitize_username("Alex", 1000), "alex")

    def test_invalid_characters_replaced(self):
        result = _sanitize_username("alex.smith@corp", 1000)
        self.assertRegex(result, r"^[a-z_][a-z0-9_-]*$")

    def test_leading_digit_gets_prefixed(self):
        result = _sanitize_username("123alex", 1000)
        self.assertTrue(result[0].isalpha() or result[0] == "_")

    def test_empty_or_unusable_falls_back_to_uid(self):
        self.assertEqual(_sanitize_username("", 1000), "user1000")
        self.assertEqual(_sanitize_username("!!!", 1000), "user1000")

    def test_result_never_exceeds_linux_username_limit(self):
        result = _sanitize_username("a" * 100, 1000)
        self.assertLessEqual(len(result), 32)


class DetectHostIdentityTests(unittest.TestCase):
    def test_returns_real_values_for_current_process(self):
        import os
        identity = detect_host_identity()
        self.assertIsInstance(identity, HostIdentity)
        self.assertEqual(identity.uid, os.getuid())
        self.assertEqual(identity.gid, os.getgid())
        self.assertTrue(identity.home.is_absolute())
        self.assertRegex(identity.name, r"^[a-z_][a-z0-9_-]*$")

    def test_identity_is_not_the_old_hardcoded_neutral_account(self):
        # A previous revision hardcoded a fixed "agentbox" identity for
        # every host; this must now reflect the real user instead.
        identity = detect_host_identity()
        self.assertNotEqual(identity.name, "agentbox")

    def test_fallback_used_when_uid_has_no_passwd_entry(self):
        from unittest import mock
        with mock.patch("pwd.getpwuid", side_effect=KeyError()):
            identity = detect_host_identity()
        self.assertRegex(identity.name, r"^[a-z_][a-z0-9_-]*$")
        self.assertTrue(identity.home.is_absolute())


if __name__ == "__main__":
    unittest.main()
