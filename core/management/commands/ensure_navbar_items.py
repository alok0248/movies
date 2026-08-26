"""Management command to create default navbar items for Watch Regions and Providers."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create Watch Regions and Providers navbar items if they do not exist'

    def handle(self, *args, **options):
        from core.models import NavbarItem

        items = [
            {
                'name': 'Watch Regions',
                'built_in_id': 'watch_regions',
                'icon': 'fas fa-globe',
                'item_type': 'built_in',
                'is_active': True,
                'order': 6,
            },
            {
                'name': 'Providers',
                'built_in_id': 'providers',
                'icon': 'fas fa-play-circle',
                'item_type': 'built_in',
                'is_active': True,
                'order': 7,
            },
        ]

        for item_data in items:
            obj, created = NavbarItem.objects.get_or_create(
                built_in_id=item_data['built_in_id'],
                defaults=item_data,
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created navbar item: {obj.name}"))
            else:
                self.stdout.write(f"Already exists: {obj.name}")
