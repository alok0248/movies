import os
import re
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'movie_portal.settings')
django.setup()

from django.contrib.auth import get_user_model
from django.test import Client

staff = get_user_model().objects.filter(is_superuser=True).first()
c = Client()
c.force_login(staff)

pages = [
    '/admin-dashboard/blocked-ips/',
    '/admin-dashboard/probes/',
    '/admin-dashboard/requests/?tab=users',
    '/admin-dashboard/audit-log/',
    '/admin-dashboard/api-errors/',
]

for url in pages:
    r = c.get(url)
    body = r.content.decode('utf-8', 'ignore')
    tables = re.findall(r'<table.*?</table>', body, re.S)
    header_counts = []
    row_counts = []
    for table in tables:
        head = re.search(r'<thead.*?</thead>', table, re.S)
        body_part = re.search(r'<tbody.*?</tbody>', table, re.S)
        if not head or not body_part:
            continue
        header_counts.append(len(re.findall(r'<th[ >]', head.group(0))))
        for row in re.findall(r'<tr[ >].*?</tr>', body_part.group(0), re.S):
            cells = len(re.findall(r'<td[ >]', row))
            span = re.search(r'colspan="(\d+)"', row)
            row_counts.append(int(span.group(1)) if span else cells)
    print('%-40s %s  headers=%s  rows=%s  chips=%d' % (
        url, r.status_code, header_counts, sorted(set(row_counts)), body.count('class="ipgeo')))
