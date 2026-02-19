"""
Tests for mailgate email processing helper functions.

Tests cover:
- extract_attachments: attachment extraction from email messages
- filter_recipients: filtering learningu.org aliases from real addresses
- resolve_aliases: PlainRedirect and ESPUser lookups for aliases
- resolve_recipients: full recipient resolution pipeline
- parse_sender_email: parsing the From header field
- lookup_sender: ESPUser lookup and group-based prioritization
- build_email_body: HTML body generation from email content
"""

from __future__ import absolute_import

import email
import email.policy
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from django.contrib.auth.models import Group
from django.test import TestCase

from esp.dbmail.mailgate_helpers import (
    build_email_body,
    extract_attachments,
    filter_recipients,
    lookup_sender,
    parse_sender_email,
    resolve_aliases,
    resolve_recipients,
)
from esp.dbmail.models import PlainRedirect
from esp.tests.util import user_role_setup
from esp.users.models import ESPUser


class ExtractAttachmentsTest(TestCase):
    """Tests for extract_attachments()."""

    def _make_message_with_attachment(self, filename, content, mimetype='application/octet-stream'):
        """Helper to create an email with a single attachment."""
        msg = EmailMessage(policy=email.policy.default)
        msg['Subject'] = 'Test'
        msg['From'] = 'test@example.com'
        msg['To'] = 'dest@example.com'
        msg.set_content('Hello body')
        maintype, subtype = mimetype.split('/', 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
        return msg

    def test_single_attachment(self):
        content = b'file contents here'
        msg = self._make_message_with_attachment('test.txt', content, 'text/plain')
        result = extract_attachments(msg)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 'test.txt')
        self.assertEqual(result[0][1], content)
        self.assertIn('text/plain', result[0][2])

    def test_multiple_attachments(self):
        msg = EmailMessage(policy=email.policy.default)
        msg['Subject'] = 'Test'
        msg['From'] = 'test@example.com'
        msg['To'] = 'dest@example.com'
        msg.set_content('Hello body')
        msg.add_attachment(b'pdf data', maintype='application', subtype='pdf', filename='doc.pdf')
        msg.add_attachment(b'image data', maintype='image', subtype='png', filename='photo.png')
        result = extract_attachments(msg)
        self.assertEqual(len(result), 2)
        filenames = [r[0] for r in result]
        self.assertIn('doc.pdf', filenames)
        self.assertIn('photo.png', filenames)

    def test_no_attachments(self):
        msg = EmailMessage(policy=email.policy.default)
        msg['Subject'] = 'Test'
        msg['From'] = 'test@example.com'
        msg['To'] = 'dest@example.com'
        msg.set_content('Hello body')
        result = extract_attachments(msg)
        self.assertEqual(result, [])


class FilterRecipientsTest(TestCase):
    """Tests for filter_recipients(): filtering learningu.org aliases."""

    def test_all_real_addresses(self):
        recipients = ['alice@gmail.com', 'bob@yahoo.com']
        real, aliases = filter_recipients(recipients)
        self.assertEqual(real, ['alice@gmail.com', 'bob@yahoo.com'])
        self.assertEqual(aliases, [])

    def test_all_aliases(self):
        recipients = ['user1@site.learningu.org', 'user2@other.learningu.org']
        real, aliases = filter_recipients(recipients)
        self.assertEqual(real, [])
        self.assertEqual(aliases, ['user1@site.learningu.org', 'user2@other.learningu.org'])

    def test_mixed_recipients(self):
        recipients = [
            'real@gmail.com',
            'alias@site.learningu.org',
            'another@yahoo.com',
            'admin@test.learningu.org',
        ]
        real, aliases = filter_recipients(recipients)
        self.assertEqual(real, ['real@gmail.com', 'another@yahoo.com'])
        self.assertEqual(aliases, ['alias@site.learningu.org', 'admin@test.learningu.org'])

    def test_empty_list(self):
        real, aliases = filter_recipients([])
        self.assertEqual(real, [])
        self.assertEqual(aliases, [])

    def test_address_without_at_symbol_discarded(self):
        recipients = ['malformed-address', 'valid@gmail.com']
        real, aliases = filter_recipients(recipients)
        self.assertEqual(real, ['valid@gmail.com'])
        self.assertEqual(aliases, [])

    def test_learningu_org_without_subdomain(self):
        """user@learningu.org should be treated as an alias since it ends with .learningu.org is not satisfied,
        but it does NOT end with '.learningu.org' (no dot before 'learningu'). It has '@' so it goes to real."""
        recipients = ['user@learningu.org']
        real, aliases = filter_recipients(recipients)
        # 'user@learningu.org' does NOT end with '.learningu.org'
        self.assertEqual(real, ['user@learningu.org'])
        self.assertEqual(aliases, [])

    def test_custom_domain(self):
        recipients = ['user@custom.org', 'other@gmail.com']
        real, aliases = filter_recipients(recipients, domain='custom.org')
        self.assertEqual(real, ['other@gmail.com'])
        self.assertEqual(aliases, ['user@custom.org'])


class ResolveAliasesTest(TestCase):
    """Tests for resolve_aliases(): PlainRedirect and ESPUser lookups."""

    def setUp(self):
        user_role_setup()

    def test_resolve_via_plain_redirect(self):
        PlainRedirect.objects.create(original='directors', destination='alice@gmail.com')
        result = resolve_aliases(['directors@site.learningu.org'])
        self.assertIn('alice@gmail.com', result)

    def test_resolve_via_plain_redirect_case_insensitive(self):
        PlainRedirect.objects.create(original='Directors', destination='alice@gmail.com')
        result = resolve_aliases(['directors@site.learningu.org'])
        self.assertIn('alice@gmail.com', result)

    def test_resolve_comma_separated_redirect(self):
        PlainRedirect.objects.create(
            original='team', destination='alice@gmail.com,bob@yahoo.com'
        )
        result = resolve_aliases(['team@site.learningu.org'])
        self.assertIn('alice@gmail.com', result)
        self.assertIn('bob@yahoo.com', result)

    def test_resolve_via_espuser(self):
        user = ESPUser.objects.create_user(
            username='jsmith', email='jsmith@gmail.com', password='password'
        )
        result = resolve_aliases(['jsmith@site.learningu.org'])
        self.assertIn('jsmith@gmail.com', result)

    def test_resolve_via_espuser_case_insensitive(self):
        ESPUser.objects.create_user(
            username='JSmith', email='jsmith@gmail.com', password='password'
        )
        result = resolve_aliases(['jsmith@site.learningu.org'])
        self.assertIn('jsmith@gmail.com', result)

    def test_redirect_to_learningu_filtered_out(self):
        """Redirects that resolve to another learningu.org address are filtered."""
        PlainRedirect.objects.create(
            original='alias', destination='other@another.learningu.org'
        )
        result = resolve_aliases(['alias@site.learningu.org'])
        self.assertEqual(result, [])

    def test_user_email_is_learningu_filtered_out(self):
        """User emails that are learningu.org addresses are filtered."""
        ESPUser.objects.create_user(
            username='testuser', email='testuser@site.learningu.org', password='password'
        )
        result = resolve_aliases(['testuser@site.learningu.org'])
        self.assertEqual(result, [])

    def test_null_redirect_destination_excluded(self):
        PlainRedirect.objects.create(original='empty', destination=None)
        result = resolve_aliases(['empty@site.learningu.org'])
        self.assertEqual(result, [])

    def test_empty_string_redirect_destination_excluded(self):
        PlainRedirect.objects.create(original='blank', destination='')
        result = resolve_aliases(['blank@site.learningu.org'])
        self.assertEqual(result, [])

    def test_no_matching_redirect_or_user(self):
        result = resolve_aliases(['nonexistent@site.learningu.org'])
        self.assertEqual(result, [])

    def test_both_redirect_and_user_resolved(self):
        """When both a PlainRedirect and an ESPUser match, both are included."""
        PlainRedirect.objects.create(original='shared', destination='redirect@gmail.com')
        ESPUser.objects.create_user(
            username='shared', email='user@gmail.com', password='password'
        )
        result = resolve_aliases(['shared@site.learningu.org'])
        self.assertIn('redirect@gmail.com', result)
        self.assertIn('user@gmail.com', result)

    def test_empty_aliases_list(self):
        result = resolve_aliases([])
        self.assertEqual(result, [])


class ResolveRecipientsTest(TestCase):
    """Tests for the full resolve_recipients() pipeline."""

    def setUp(self):
        user_role_setup()

    def test_mixed_real_and_aliases(self):
        ESPUser.objects.create_user(
            username='teacher1', email='teacher@school.edu', password='password'
        )
        recipients = ['parent@gmail.com', 'teacher1@site.learningu.org']
        result = resolve_recipients(recipients)
        self.assertIn('parent@gmail.com', result)
        self.assertIn('teacher@school.edu', result)

    def test_empty_recipients_returns_empty(self):
        result = resolve_recipients([])
        self.assertEqual(result, [])

    def test_all_aliases_resolve_to_nothing(self):
        """When all aliases are unresolvable, returns empty list."""
        result = resolve_recipients(['nobody@site.learningu.org'])
        self.assertEqual(result, [])

    def test_all_real_addresses_pass_through(self):
        recipients = ['a@gmail.com', 'b@yahoo.com', 'c@school.edu']
        result = resolve_recipients(recipients)
        self.assertEqual(result, recipients)


class ParseSenderEmailTest(TestCase):
    """Tests for parse_sender_email(): parsing the From header."""

    def test_simple_email(self):
        self.assertEqual(parse_sender_email('user@example.com'), 'user@example.com')

    def test_display_name_format(self):
        self.assertEqual(
            parse_sender_email('John Doe <john@example.com>'),
            'john@example.com'
        )

    def test_none_from_field(self):
        self.assertIsNone(parse_sender_email(None))

    def test_empty_string(self):
        self.assertIsNone(parse_sender_email(''))

    def test_whitespace_only(self):
        self.assertIsNone(parse_sender_email('   '))

    def test_multiple_senders_raises(self):
        with self.assertRaises(AttributeError):
            parse_sender_email('user1@example.com,user2@example.com')

    def test_multiple_senders_with_spaces_raises(self):
        with self.assertRaises(AttributeError):
            parse_sender_email('user1@example.com, user2@example.com')

    def test_display_name_with_angle_brackets(self):
        self.assertEqual(
            parse_sender_email('"Smith, John" <jsmith@example.com>'),
            'jsmith@example.com'
        )

    def test_strips_whitespace(self):
        self.assertEqual(
            parse_sender_email('  user@example.com  '),
            'user@example.com'
        )


class LookupSenderTest(TestCase):
    """Tests for lookup_sender(): ESPUser lookup and group prioritization."""

    EMAIL_HOST_SENDER = 'test.learningu.org'

    def setUp(self):
        user_role_setup()

    def test_lookup_by_username_for_site_domain(self):
        user = ESPUser.objects.create_user(
            username='jsmith', email='jsmith@gmail.com', password='password'
        )
        result = lookup_sender('jsmith@test.learningu.org', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, user)

    def test_lookup_by_email_for_external_domain(self):
        user = ESPUser.objects.create_user(
            username='jsmith', email='jsmith@gmail.com', password='password'
        )
        result = lookup_sender('jsmith@gmail.com', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, user)

    def test_case_insensitive_username_lookup(self):
        user = ESPUser.objects.create_user(
            username='JSmith', email='jsmith@gmail.com', password='password'
        )
        result = lookup_sender('jsmith@test.learningu.org', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, user)

    def test_case_insensitive_email_lookup(self):
        user = ESPUser.objects.create_user(
            username='jsmith', email='JSmith@Gmail.COM', password='password'
        )
        result = lookup_sender('jsmith@gmail.com', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, user)

    def test_no_matching_user_returns_none(self):
        result = lookup_sender('nobody@example.com', self.EMAIL_HOST_SENDER)
        self.assertIsNone(result)

    def test_no_matching_username_returns_none(self):
        result = lookup_sender('ghost@test.learningu.org', self.EMAIL_HOST_SENDER)
        self.assertIsNone(result)

    def test_single_user_returned_directly(self):
        user = ESPUser.objects.create_user(
            username='solo', email='solo@gmail.com', password='password'
        )
        result = lookup_sender('solo@gmail.com', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, user)

    def test_multiple_users_admin_preferred(self):
        """Administrator account should be preferred over others."""
        teacher = ESPUser.objects.create_user(
            username='user_t', email='shared@gmail.com', password='password'
        )
        teacher.makeRole('Teacher')
        admin = ESPUser.objects.create_user(
            username='user_a', email='shared@gmail.com', password='password'
        )
        admin.makeRole('Administrator')
        result = lookup_sender('shared@gmail.com', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, admin)

    def test_multiple_users_teacher_preferred_over_student(self):
        """Teacher account should be preferred over Student."""
        student = ESPUser.objects.create_user(
            username='user_s', email='dup@gmail.com', password='password'
        )
        student.makeRole('Student')
        teacher = ESPUser.objects.create_user(
            username='user_t', email='dup@gmail.com', password='password'
        )
        teacher.makeRole('Teacher')
        result = lookup_sender('dup@gmail.com', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, teacher)

    def test_multiple_users_volunteer_preferred_over_student(self):
        """Volunteer account should be preferred over Student."""
        student = ESPUser.objects.create_user(
            username='user_s2', email='vol@gmail.com', password='password'
        )
        student.makeRole('Student')
        volunteer = ESPUser.objects.create_user(
            username='user_v', email='vol@gmail.com', password='password'
        )
        volunteer.makeRole('Volunteer')
        result = lookup_sender('vol@gmail.com', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, volunteer)

    def test_multiple_users_no_standard_group_oldest_chosen(self):
        """If no users match standard groups, the oldest account is returned."""
        u1 = ESPUser.objects.create_user(
            username='old_user', email='nogroup@gmail.com', password='password'
        )
        u2 = ESPUser.objects.create_user(
            username='new_user', email='nogroup@gmail.com', password='password'
        )
        result = lookup_sender('nogroup@gmail.com', self.EMAIL_HOST_SENDER)
        # oldest by date_joined; since u1 was created first, it should be selected
        self.assertEqual(result, u1)

    def test_multiple_same_group_oldest_chosen(self):
        """Among multiple users in the same group, the oldest account is returned."""
        s1 = ESPUser.objects.create_user(
            username='student_old', email='students@gmail.com', password='password'
        )
        s1.makeRole('Student')
        s2 = ESPUser.objects.create_user(
            username='student_new', email='students@gmail.com', password='password'
        )
        s2.makeRole('Student')
        result = lookup_sender('students@gmail.com', self.EMAIL_HOST_SENDER)
        self.assertEqual(result, s1)


class BuildEmailBodyTest(TestCase):
    """Tests for build_email_body(): HTML body generation."""

    def _make_plain_message(self, text):
        msg = EmailMessage(policy=email.policy.default)
        msg['Subject'] = 'Test'
        msg['From'] = 'test@example.com'
        msg['To'] = 'dest@example.com'
        msg.set_content(text)
        return msg

    def _make_html_message(self, html_content):
        msg = EmailMessage(policy=email.policy.default)
        msg['Subject'] = 'Test'
        msg['From'] = 'test@example.com'
        msg['To'] = 'dest@example.com'
        msg.set_content('fallback text')
        msg.add_alternative(html_content, subtype='html')
        return msg

    def test_plain_text_is_escaped(self):
        msg = self._make_plain_message('Hello <world> & "friends"')
        body = build_email_body(msg)
        self.assertIn('&lt;world&gt;', body)
        self.assertIn('&amp;', body)
        self.assertIn('<html>', body)
        self.assertIn('</html>', body)

    def test_html_content_preserved(self):
        html_content = '<p>Hello <b>world</b></p>'
        msg = self._make_html_message(html_content)
        body = build_email_body(msg)
        self.assertIn('<p>Hello <b>world</b></p>', body)
        self.assertIn('<html>', body)

    def test_body_has_html_structure(self):
        msg = self._make_plain_message('Simple text')
        body = build_email_body(msg)
        self.assertIn('<html>', body)
        self.assertIn('<head>', body)
        self.assertIn('<body>', body)
        self.assertIn('</body>', body)
        self.assertIn('</html>', body)


class IntegrationTest(TestCase):
    """Integration-style tests combining multiple helper functions."""

    EMAIL_HOST_SENDER = 'test.learningu.org'

    def setUp(self):
        user_role_setup()

    def test_full_flow_with_mixed_recipients_and_valid_sender(self):
        """Simulate a class list email with mixed recipients and a valid sender."""
        teacher = ESPUser.objects.create_user(
            username='teacher1', email='teacher@school.edu', password='password'
        )
        teacher.makeRole('Teacher')

        PlainRedirect.objects.create(original='directors', destination='director@school.edu')

        recipients = [
            'parent@gmail.com',
            'directors@site.learningu.org',
            'teacher1@site.learningu.org',
        ]

        resolved = resolve_recipients(recipients)
        self.assertIn('parent@gmail.com', resolved)
        self.assertIn('director@school.edu', resolved)
        self.assertIn('teacher@school.edu', resolved)

        sender_email = parse_sender_email('Teacher One <teacher1@test.learningu.org>')
        self.assertEqual(sender_email, 'teacher1@test.learningu.org')

        sender = lookup_sender(sender_email, self.EMAIL_HOST_SENDER)
        self.assertEqual(sender, teacher)

    def test_full_flow_unknown_sender_rejected(self):
        """Sender with no account should be rejected (returns None)."""
        sender_email = parse_sender_email('stranger@example.com')
        sender = lookup_sender(sender_email, self.EMAIL_HOST_SENDER)
        self.assertIsNone(sender)

    def test_full_flow_empty_from_rejected(self):
        """Empty from field should be handled gracefully."""
        sender_email = parse_sender_email('')
        self.assertIsNone(sender_email)

    def test_full_flow_all_aliases_unresolvable_yields_empty_recipients(self):
        """When all recipients are unresolvable aliases, recipient list is empty."""
        recipients = ['ghost@site.learningu.org', 'phantom@other.learningu.org']
        resolved = resolve_recipients(recipients)
        self.assertEqual(resolved, [])
