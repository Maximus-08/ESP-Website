"""
Helper functions for mailgate email processing.

These functions extract the core recipient resolution, sender validation,
and attachment extraction logic from mailgate.py into testable units.
"""

from __future__ import absolute_import

import html
import itertools
import logging

from django.db.models.functions import Lower

from esp.dbmail.models import PlainRedirect
from esp.users.models import ESPUser

logger = logging.getLogger('esp.mailgate')

DOMAIN = '.learningu.org'


def extract_attachments(msg):
    """
    Extract attachments from an email.message.EmailMessage object.

    Returns a list of (filename, content, mimetype) tuples suitable for
    passing to Django's send_mail as the `attachments` parameter.
    """
    attachments = []
    for part in msg.iter_attachments():
        filename = part.get_filename()
        content = part.get_payload(decode=True)
        mimetype = part.get_content_type()
        if content:
            attachments.append((filename, content, mimetype))
        else:
            logger.info("No content in attachment {}".format(filename))
    return attachments


def filter_recipients(recipients, domain=DOMAIN):
    """
    Split a list of recipient email addresses into two groups:
    - real_recipients: addresses that do NOT end with the given domain
    - aliases: addresses that DO end with the given domain

    Addresses without an '@' symbol are logged and discarded.

    Args:
        recipients: iterable of email address strings
        domain: the domain suffix to filter on (default: '.learningu.org')

    Returns:
        (real_recipients, aliases) tuple of two lists
    """
    real_recipients = []
    aliases = []
    for recipient in recipients:
        if recipient.endswith(domain):
            aliases.append(recipient)
        elif '@' in recipient:
            real_recipients.append(recipient)
        else:
            logger.warning('Email address without `@` symbol: `{}`'.format(recipient))
    return real_recipients, aliases


def resolve_aliases(aliases):
    """
    Resolve learningu.org aliases to real email addresses by looking up
    PlainRedirect objects and ESPUser accounts.

    For each alias (e.g., 'username@site.learningu.org'), the local part
    (before '@') is looked up in:
    1. PlainRedirect.original (case-insensitive)
    2. ESPUser.username (case-insensitive)

    Any redirect destinations that are themselves learningu.org addresses
    are filtered out.

    Args:
        aliases: list of email addresses ending in .learningu.org

    Returns:
        list of resolved real email addresses
    """
    local_parts = [x.split('@')[0].lower() for x in aliases]

    # Look up PlainRedirect entries
    redirects = PlainRedirect.objects.annotate(
        original_lower=Lower("original")
    ).filter(
        original_lower__in=local_parts
    ).exclude(
        destination__isnull=True
    ).exclude(
        destination=''
    )

    # Flatten comma-separated redirect destinations
    redirect_addresses = list(itertools.chain.from_iterable(
        x.destination.split(',') if x.destination else []
        for x in redirects
    ))

    # Look up ESPUser entries
    users = ESPUser.objects.annotate(
        username_lower=Lower("username")
    ).filter(
        username_lower__in=local_parts
    )
    user_addresses = [x.email for x in users]

    # Filter out any addresses that are still learningu.org aliases
    resolved = []
    for address in redirect_addresses + user_addresses:
        address = address.strip()
        if address and not address.endswith(DOMAIN):
            resolved.append(address)

    return resolved


def resolve_recipients(recipients, domain=DOMAIN):
    """
    Full recipient resolution pipeline: filter out aliases from the recipient
    list, resolve them via PlainRedirect and ESPUser lookups, and return
    the combined list of real email addresses.

    Args:
        recipients: iterable of email address strings
        domain: the domain suffix to filter on

    Returns:
        list of resolved real email addresses, or empty list if none found
    """
    real_recipients, aliases = filter_recipients(recipients, domain)
    resolved_from_aliases = resolve_aliases(aliases)
    return real_recipients + resolved_from_aliases


def parse_sender_email(from_field):
    """
    Parse the 'From' header field and extract a single email address.

    Handles formats like:
    - 'user@example.com'
    - 'Display Name <user@example.com>'
    - '' or None (empty)
    - Comma-separated multiple addresses (raises AttributeError)

    Args:
        from_field: the raw 'From' header string, or None

    Returns:
        The extracted email address string, or None if the field is empty.

    Raises:
        AttributeError: if more than one sender is found
    """
    if not from_field:
        return None

    addresses = from_field.split(',')

    # Check for empty/blank addresses
    if not addresses or (len(addresses) == 1 and not addresses[0].strip()):
        return None

    if len(addresses) != 1:
        raise AttributeError("More than one sender: `{}`".format(addresses))

    email_address = addresses[0].strip()

    # Extract email from "Display Name <email>" format
    if '<' in email_address and '>' in email_address:
        email_address = email_address.split('<')[1].split('>')[0]

    return email_address


def lookup_sender(email_address, email_host_sender):
    """
    Look up an ESPUser account for the given email address.

    If the address ends with the site's EMAIL_HOST_SENDER domain, lookup is
    done by username (the local part before '@'). Otherwise, lookup is by
    the full email address.

    When multiple accounts match, they are prioritized by role:
    Administrator > Teacher > Volunteer > Student > Educator,
    then by earliest creation date.

    Args:
        email_address: the sender's email address
        email_host_sender: the site's EMAIL_HOST_SENDER setting

    Returns:
        An ESPUser instance, or None if no matching account is found.
    """
    if email_address.endswith(email_host_sender):
        users = ESPUser.objects.filter(
            username__iexact=email_address.split('@')[0]
        ).order_by('date_joined')
    else:
        users = ESPUser.objects.filter(
            email__iexact=email_address
        ).order_by('date_joined')

    users = list(users)

    if len(users) == 0:
        logger.warning(
            'Received email from {}, which is not associated with a user'.format(email_address)
        )
        return None

    if len(users) == 1:
        return users[0]

    # Multiple users: prioritize by group
    sender = users[0]  # default fallback to oldest account
    for group_name in ['Administrator', 'Teacher', 'Volunteer', 'Student', 'Educator']:
        group_users = [x for x in users if x.groups.filter(name=group_name).exists()]
        if len(group_users) > 0:
            sender = group_users[0]
            break

    logger.debug("Group selection: {} -> {}".format(group_name, group_users))
    return sender


def build_email_body(message):
    """
    Convert an email message into an HTML body string.

    If the original message is plain text, it is HTML-escaped before
    being wrapped in an HTML structure.

    Args:
        message: an email.message.EmailMessage object

    Returns:
        An HTML string suitable for sending via send_mail
    """
    message_content = message.get_body(preferencelist=('html', 'plain')).get_content()

    if message.get_content_type() != 'text/html':
        content = html.escape(message_content)
    else:
        content = message_content

    return '''\
<html>
  <head>
    <meta charset="UTF-8">
    <title>Email Content</title>
  </head>
  <body>
    {}
  </body>
</html>'''.format(content)
