"""Column layouts for the "All Users & Devices" tables.

Every tab of ``android_app_users_full`` is a wide table, and which columns an
operator cares about differs from person to person (an ops admin watches
heartbeats, a billing admin watches plans). This module is the single place
that knows:

* which columns each tab has (``TAB_SPECS``),
* which ones a given admin has chosen and in what order
  (``AdminTableLayout``, one row per admin + tab),
* how a clickable header sorts that tab's queryset (``SORT_FIELDS``).

The templates write every column a tab has, in one fixed order, and mark the
table with ``data-tab-cols``. The saved layout is then applied **on the server**
before the page is sent (``filter_table_columns``), so a column an admin hid is
not in the HTML at all and cannot flash on screen while the page loads. The
browser script (``applyTableLayout`` in ``_users_full_cols_js.html``) only keeps
the table in step with the picker while it is open, and the panel is re-rendered
from the server once a layout is saved.
"""

import re
from html.parser import HTMLParser

from .models import AdminTableLayout

# Fallback wording when a tab has no saved layout: this is what a fresh admin
# sees, and the order the table markup is written in.
# The page keeps four tabs: every account in one table (verified, signed out and
# app version are filters on it), every device in another (the devices gone
# quiet are a filter there, with the same bulk delete), the visit log and the
# per-period counts. The tables carry the columns of the tabs they replaced.
TAB_SPECS = {
    'accounts': [
        {'key': 'user', 'label': 'User', 'sort': 'display_name'},
        {'key': 'email', 'label': 'Email', 'sort': 'email'},
        {'key': 'verified', 'label': 'Email verified', 'sort': 'is_email_verified'},
        {'key': 'joined', 'label': 'Registered', 'sort': 'created_at'},
        {'key': 'status', 'label': 'Status (live)'},
        {'key': 'device', 'label': 'Device', 'sort': 'device_model'},
        {'key': 'app', 'label': 'App version', 'sort': 'app_version'},
        {'key': 'build', 'label': 'Build', 'sort': 'build_number'},
        {'key': 'last_sync', 'label': 'Last sync', 'sort': 'last_synced_at'},
        {'key': 'app_device', 'label': 'Install record'},
        {'key': 'account', 'label': 'Login account'},
        {'key': 'subscription', 'label': 'Subscription', 'sort': 'plan'},
        {'key': 'library', 'label': 'Library / cloud'},
        {'key': 'access', 'label': 'Access', 'sort': 'block_mode'},
        {'key': 'manage', 'label': 'Manage'},
    ],
    'uniquedevices': [
        {'key': 'check', 'label': ''},
        {'key': 'index', 'label': '#'},
        {'key': 'device', 'label': 'Device'},
        {'key': 'account', 'label': 'Account'},
        {'key': 'status', 'label': 'Status'},
        {'key': 'ip', 'label': 'IP'},
        {'key': 'subscription', 'label': 'Subscription'},
        {'key': 'first_registered', 'label': 'First registered'},
        {'key': 'last_seen', 'label': 'Last seen online'},
        {'key': 'silent', 'label': 'Silent'},
        {'key': 'visits', 'label': 'Visits'},
        {'key': 'history', 'label': 'History'},
    ],
    'visits': [
        {'key': 'time', 'label': 'Time'},
        {'key': 'device_id', 'label': 'Device ID'},
        {'key': 'account', 'label': 'Account'},
        {'key': 'model', 'label': 'Model'},
        {'key': 'os', 'label': 'OS'},
        {'key': 'build', 'label': 'Build ID'},
        {'key': 'ip', 'label': 'IP address'},
        {'key': 'request', 'label': 'Request'},
    ],
    'dailyusers': [
        {'key': 'period', 'label': 'Period'},
        {'key': 'active', 'label': 'Active'},
        {'key': 'new', 'label': 'New'},
        {'key': 'registered', 'label': 'Registered'},
        {'key': 'converted', 'label': 'Converted'},
        {'key': 'share', 'label': 'Share of the peak'},
        {'key': 'change', 'label': 'Change'},
    ],
}

# ORM fields a header click may sort by. A column that is not listed here is
# not sortable (it is computed per row, or it is an action cell).
SORT_FIELDS = {
    'accounts': {
        'display_name': 'display_name',
        'email': 'email',
        'is_email_verified': 'is_email_verified',
        'created_at': 'created_at',
        'plan': 'plan',
        'valid_until': 'valid_until',
        'block_mode': 'block_mode',
        'device_model': 'device_model',
        'app_version': 'app_version',
        'build_number': 'build_number',
        'last_synced_at': 'last_synced_at',
    },
}

DEFAULT_SORT = {'accounts': '-last_synced_at'}


def specs(tab):
    """Every column a tab can show, in its default order."""
    return [dict(col) for col in TAB_SPECS.get(tab, [])]


def default_keys(tab):
    return [col['key'] for col in TAB_SPECS.get(tab, [])]


def _valid_keys(tab, keys):
    """Keep only keys the tab actually has, drop repeats, keep the given order."""
    known = default_keys(tab)
    out = []
    for key in keys or []:
        key = str(key).strip()
        if key in known and key not in out:
            out.append(key)
    return out


def saved_keys_map(user, tabs):
    """Every layout this admin has saved for ``tabs``, in one query.

    Tabs without a saved (or still valid) layout get their default order, so the
    result always has an entry per requested tab.
    """
    tabs = list(tabs)
    stored = {}
    if user is not None and getattr(user, 'is_authenticated', False) and tabs:
        try:
            stored = dict(AdminTableLayout.objects.filter(user=user, tab__in=tabs)
                          .values_list('tab', 'columns'))
        except Exception:
            # A layout is a preference: if its table is missing (code deployed
            # ahead of `migrate`), the page still has to render with defaults.
            stored = {}
    return {t: (_valid_keys(t, stored.get(t)) or default_keys(t)) for t in tabs}


def saved_keys(user, tab):
    """The admin's own column order for this tab, or the tab's default order."""
    return saved_keys_map(user, [tab])[tab]


def save_keys(user, tab, keys):
    """Store one admin's column order for one tab and return it back."""
    keys = _valid_keys(tab, keys) or default_keys(tab)
    AdminTableLayout.objects.update_or_create(
        user=user, tab=tab, defaults={'columns': keys})
    return keys


def columns_for(user, tab, sort_key='', sort_dir='asc', base_params=None):
    """Columns to render: label, whether it can sort, and its header link.

    ``base_params`` is a dict of the current query string values the sort link
    has to keep (tab, q, filters). The template only has to print ``url``.
    """
    saved = saved_keys(user, tab)
    sortable = SORT_FIELDS.get(tab, {})
    params = dict(base_params or {})
    columns = []
    for col in specs(tab):
        field = sortable.get(col.get('sort') or '')
        entry = {
            'key': col['key'],
            'label': col['label'],
            'visible': col['key'] in saved,
            'sortable': bool(field),
            'active': bool(field) and sort_key == (col.get('sort') or ''),
            'dir': sort_dir,
        }
        if field:
            nxt = 'desc' if entry['active'] and sort_dir == 'asc' else 'asc'
            link = dict(params)
            link['tab'] = tab
            link['sort'] = col['sort']
            link['dir'] = nxt
            link.pop('page', None)
            entry['url'] = '&'.join(
                f'{k}={v}' for k, v in link.items() if v not in (None, ''))
        columns.append(entry)
    return columns


def parse_sort(tab, sort_key, sort_dir):
    """Validate ?sort=/?dir= against the tab and return (key, direction)."""
    sortable = SORT_FIELDS.get(tab, {})
    key = (sort_key or '').strip()
    direction = 'desc' if (sort_dir or '').strip().lower() == 'desc' else 'asc'
    if key not in sortable:
        default = DEFAULT_SORT.get(tab, '')
        if default.startswith('-'):
            return default[1:], 'desc'
        return default, 'asc'
    return key, direction


def order_queryset(qs, tab, sort_key, sort_dir):
    """Apply the validated sort to a queryset (no-op when unsortable)."""
    field = SORT_FIELDS.get(tab, {}).get(sort_key or '')
    if not field:
        return qs
    prefix = '-' if sort_dir == 'desc' else ''
    # A stable tie-breaker keeps pagination from repeating rows.
    return qs.order_by(prefix + field, '-id')


def table_cells_attr(tab, keys=None):
    """``data-tab-cols`` value: the columns the cells are in, comma separated."""
    return ','.join(keys if keys is not None else default_keys(tab))


_COLSPAN = re.compile(r'(colspan\s*=\s*)("[^"]*"|\'[^\']*\'|[^\s>]+)', re.I)


class _ColumnFilter(HTMLParser):
    """Rewrites the rows of every ``<table data-tab-cols>`` to one layout.

    Cells are matched to columns by their ``data-col`` attribute when they have
    one and by position otherwise (the markup writes them in the tab's default
    order). Hidden columns are dropped, kept columns are emitted in the saved
    order, and everything else in the document is copied through unchanged.

    A row whose cell count is not the tab's column count (the single spanning
    "nothing here" cell) is left whole, with its colspan corrected to the number
    of visible columns.
    """

    _CELLS = ('td', 'th')

    def __init__(self, layout_keys, spec_keys):
        HTMLParser.__init__(self, convert_charrefs=False)
        self.layout = list(layout_keys)
        self.spec = list(spec_keys)
        self.out = []
        self.tables = 0          # open <table> elements
        self.mapped = None       # depth of the table being rewritten
        self.row = None          # the row being collected
        self.cell = None         # the cell being collected
        self.found = False       # saw a table with data-tab-cols

    # ---------------------------------------------------------------- output
    def _add(self, text):
        if self.cell is not None:
            self.cell['parts'].append(text)
        elif self.row is not None:
            self.row['parts'].append(text)
        else:
            self.out.append(text)

    def _cell_html(self, cell, spans=False):
        raw = cell['raw']
        if spans:
            raw = _COLSPAN.sub(
                lambda m: '%s"%d"' % (m.group(1), len(self.layout)), raw, count=1)
        return raw + ''.join(cell['parts']) + '</%s>' % cell['tag']

    def _render_row(self, row):
        cells = [p for p in row['parts'] if isinstance(p, dict)]
        if cells and len(cells) == len(self.spec):
            by_key = {}
            for index, cell in enumerate(cells):
                key = cell['col'] or (self.spec[index]
                                      if index < len(self.spec) else None)
                if key:
                    by_key.setdefault(key, cell)
            body = ''.join(self._cell_html(by_key[key])
                           for key in self.layout if key in by_key)
            return row['raw'] + body + '</tr>'
        # A spanning row (or any irregular one): keep it whole so its message
        # still reads across the table, with the span narrowed to the columns
        # that are actually on screen.
        body = ''.join(self._cell_html(p, spans=True) if isinstance(p, dict) else p
                       for p in row['parts'])
        return row['raw'] + body + '</tr>'

    # ---------------------------------------------------------------- parser
    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text() or '<%s>' % tag
        if tag == 'table':
            self.tables += 1
            if self.mapped is None and 'data-tab-cols' in dict(attrs):
                self.mapped = self.tables
                self.found = True
            self._add(raw)
            return
        if self.cell is not None:
            if tag == self.cell['tag']:
                self.cell['same'] += 1
            self.cell['parts'].append(raw)
            return
        if self.mapped == self.tables:
            if tag == 'tr':
                self.row = {'raw': raw, 'parts': []}
                return
            if tag in self._CELLS:
                self.cell = {'tag': tag, 'raw': raw, 'same': 0, 'parts': [],
                             'col': (dict(attrs) or {}).get('data-col')}
                return
        self._add(raw)

    def handle_startendtag(self, tag, attrs):
        self._add(self.get_starttag_text() or '<%s/>' % tag)

    def handle_endtag(self, tag):
        if tag == 'table':
            if self.mapped == self.tables:
                self.mapped = None
            self.tables = max(0, self.tables - 1)
            self._add('</table>')
            return
        if self.cell is not None:
            if tag == self.cell['tag']:
                if self.cell['same']:
                    self.cell['same'] -= 1
                else:
                    # The closing tag is added by _cell_html, once, when the cell
                    # is written back out in its new position.
                    cell, self.cell = self.cell, None
                    if self.row is not None:
                        self.row['parts'].append(cell)
                    else:
                        # A stray cell outside any row: pass it through as it is.
                        self.out.append(self._cell_html(cell))
                    return
            self.cell['parts'].append('</%s>' % tag)
            return
        if tag == 'tr' and self.row is not None:
            row, self.row = self.row, None
            self.out.append(self._render_row(row))
            return
        self._add('</%s>' % tag)

    def handle_data(self, data):
        self._add(data)

    def handle_entityref(self, name):
        self._add('&%s;' % name)

    def handle_charref(self, name):
        self._add('&#%s;' % name)

    def handle_comment(self, data):
        self._add('<!--%s-->' % data)

    def handle_decl(self, decl):
        self._add('<!%s>' % decl)

    def handle_pi(self, data):
        self._add('<?%s>' % data)

    def unknown_decl(self, data):
        self._add('<![%s]>' % data)


def filter_table_columns(html, layout_keys, spec_keys):
    """Apply a saved layout to already-rendered markup.

    Returns ``html`` untouched when the layout is the tab's default order (the
    common case, and the reason this costs nothing for an admin who never opened
    the picker), when the markup has no matching table, or if parsing fails —
    the page must render either way.
    """
    layout_keys = list(layout_keys or [])
    spec_keys = list(spec_keys or [])
    if not layout_keys or not html or layout_keys == spec_keys:
        return html
    parser = _ColumnFilter(layout_keys, spec_keys)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html
    if not parser.found:
        return html
    return ''.join(parser.out)
