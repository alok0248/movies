"""Read or change the server-driven app config served by ``/api/user/sync``.

    manage.py app_config --show
    manage.py app_config --flag downloads=off --message "Downloads paused"
    manage.py app_config --min-build 600
    manage.py app_config --reset

The payload is one JSON object; apps merge it over their own defaults and an app
too old to know a key ignores it. A flag flipped to ``off`` is the kill-switch:
new builds stop offering that feature on their next sync, without a release.

Changes are cached for a minute at most — saving through the admin or this
command clears the cache immediately, so a flip takes effect on the next request.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from core.models import AppRemoteConfig


DEFAULT_FLAGS = ['downloads', 'playback', 'signups', 'search']


class Command(BaseCommand):
    help = 'Show or edit the remote config / feature flags served to the apps.'

    def add_arguments(self, parser):
        parser.add_argument('--show', action='store_true', help='Print the stored payload (default).')
        parser.add_argument('--flag', action='append', default=[],
                            help='name=on|off — repeatable. Also accepts name=true/false/1/0.')
        parser.add_argument('--min-build', type=int, default=None,
                            help='Oldest build allowed to keep working (apps older than this are told to update).')
        parser.add_argument('--message', default=None,
                            help='Banner text for the apps ("" clears it).')
        parser.add_argument('--set', action='append', default=[],
                            help='key=value for any other top-level key — repeatable.')
        parser.add_argument('--reset', action='store_true', help='Replace the payload with {}.')

    def _bool(self, raw):
        value = raw.strip().lower()
        if value in ('on', 'true', '1', 'yes', ''):
            return True
        if value in ('off', 'false', '0', 'no'):
            return False
        raise CommandError('not a boolean: %r (use on/off)' % raw)

    def handle(self, *args, **options):
        conf = AppRemoteConfig.get_config()
        created = False
        if conf is None:
            if not (options['flag'] or options['set'] or options['reset']
                    or options['min_build'] is not None or options['message'] is not None):
                self.stdout.write('No remote config yet. The apps are running on their built-in defaults.')
                self.stdout.write('Create one with: manage.py app_config --flag downloads=on')
                return
            conf = AppRemoteConfig.objects.create(payload='{}', updated_by='app_config')
            created = True

        if options['reset']:
            conf.payload = '{}'
        payload = {} if options['reset'] else conf.as_dict()

        if options['flag']:
            flags = payload.get('flags')
            if not isinstance(flags, dict):
                flags = {}
            for item in options['flag']:
                if '=' not in item:
                    raise CommandError('--flag needs name=on|off, got %r' % item)
                name, raw = item.split('=', 1)
                flags[name.strip()] = self._bool(raw)
            payload['flags'] = flags

        if options['min_build'] is not None:
            payload['min_supported_build'] = options['min_build']

        if options['message'] is not None:
            if options['message']:
                payload['message'] = options['message']
            else:
                payload.pop('message', None)

        for item in options['set']:
            if '=' not in item:
                raise CommandError('--set needs key=value, got %r' % item)
            key, raw = item.split('=', 1)
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            payload[key.strip()] = value

        if options['flag'] or options['set'] or options['reset'] or options['message'] is not None \
                or options['min_build'] is not None:
            conf.payload = json.dumps(payload, indent=2, sort_keys=True)
            conf.updated_by = 'app_config'
            conf.clean()          # refuses a payload the apps could not parse
            conf.save()
            self.stdout.write(self.style.SUCCESS(
                '%s remote config saved' % ('Created' if created else 'Updated')))

        self.stdout.write(json.dumps(conf.as_dict(), indent=2, sort_keys=True))
        flags = conf.flags()
        if not flags:
            self.stdout.write('No flags flipped yet — every feature is on unless the app says otherwise.')
        else:
            self.stdout.write('Kill-switches currently OFF: %s'
                              % (', '.join(sorted(k for k, v in flags.items() if v is False)) or 'none'))
