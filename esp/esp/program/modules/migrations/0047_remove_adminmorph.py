import json
import os

from django.db import migrations

# State is persisted to a JSON file so that both same-process and
# cross-process reverse migrations can restore per-program M2M links.
# The module-level mutable global previously used for same-process
# round-trips has been removed: it was fragile for non-standard migration
# runners and masked file-write failures that should be fatal.
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '.adminmorph_state.json')


def remove_adminmorph_programmodule(apps, schema_editor):
    """
    Delete the ProgramModule registry entry for AdminMorph.
    Before deleting, record which programs had AdminMorph installed so that
    the reverse migration can restore those M2M links if needed.

    Handles the case where dirty data left multiple rows with
    handler='AdminMorph' by iterating all of them rather than calling .get().
    """
    ProgramModule = apps.get_model('program', 'ProgramModule')
    Program = apps.get_model('program', 'Program')

    adminmorph_qs = ProgramModule.objects.filter(handler='AdminMorph')

    # Collect affected program IDs across *all* matching rows to guard against
    # MultipleObjectsReturned on dirty data with duplicate handler entries.
    seen_ids = set()
    for am_module in adminmorph_qs:
        for pid in Program.objects.filter(modules=am_module).values_list('id', flat=True):
            seen_ids.add(pid)

    ids = sorted(seen_ids)

    # Persist to disk so the reverse migration (even in a separate process)
    # can restore the M2M links rather than silently dropping them.  If the
    # write fails AND there are programs to restore, raise so the migration
    # does not silently destroy unrecoverable state.
    try:
        with open(_STATE_FILE, 'w') as fh:
            json.dump({'program_ids': ids}, fh)
    except OSError:
        if ids:
            raise  # Don't delete rows whose state cannot be saved.
        # No programs were affected; nothing to restore on rollback.

    # Cascades to remove it from any Program.modules M2M relations automatically.
    adminmorph_qs.delete()


def restore_adminmorph_programmodule(apps, schema_editor):
    """
    Reverse of remove_adminmorph_programmodule.

    Re-inserts the ProgramModule registry entry and re-links every program
    that previously had AdminMorph installed.  The program IDs are read from
    the persisted JSON file (both same-process and cross-process rollback),
    so the M2M links are correctly restored in either scenario.
    """
    ProgramModule = apps.get_model('program', 'ProgramModule')
    Program = apps.get_model('program', 'Program')

    am, _ = ProgramModule.objects.get_or_create(
        handler='AdminMorph',
        defaults={
            'link_title': 'Morph into User',
            'admin_title': 'User Morphing Capability',
            'module_type': 'manage',
            'seq': 34,
            'choosable': 1,
        },
    )

    # Read program IDs from the persisted state file.  The file-based approach
    # works for both same-process and cross-process rollbacks without relying
    # on fragile module-level mutable global state.
    ids_to_restore = []
    try:
        with open(_STATE_FILE) as fh:
            data = json.load(fh)
        ids_to_restore = data.get('program_ids', [])
    except (OSError, ValueError):
        ids_to_restore = []

    for program in Program.objects.filter(id__in=ids_to_restore):
        program.modules.add(am)

    # Clean up the state file now that it has been consumed.
    try:
        os.remove(_STATE_FILE)
    except OSError:
        pass

class Migration(migrations.Migration):

    dependencies = [
        ('modules', '0046_auto_20260106_2204'),
        ('program', '0001_initial'),
    ]

    operations = [
        # Clean up the ProgramModule registry row first (and any Program.modules M2M refs)
        migrations.RunPython(
            remove_adminmorph_programmodule,
            restore_adminmorph_programmodule,
        ),
        # Remove the Django proxy model
        migrations.DeleteModel(
            name='AdminMorph',
        ),
    ]
