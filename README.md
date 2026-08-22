# Movie Streaming Portal

A Django-based movie and TV show streaming platform with a modern, Netflix-style UI.

## Features

- Multiple data source support: TMDB API, TMDB Database (extracted), Local Database, Xtream
- Browse popular, top-rated, and upcoming movies and TV shows
- Search for movies and TV shows
- Genre-based browsing
- Watchlist for authenticated users
- User authentication with password reset
- Live TV (sample implementation)
- Admin dashboard for managing content rows and site settings
- Responsive design
- Context-aware genre navigation
- Watchlist warnings for unauthenticated users

## Tech Stack

- **Backend**: Django 6.0.6
- **Database**: SQLite (default), supports PostgreSQL, Oracle, SQL Server
- **APIs**: The Movie Database (TMDB), CodeSpecters
- **Frontend**: HTML5, CSS3, Bootstrap 5
- **Python**: 3.14

## Project Structure

```
movies/
├── core/                    # Main app
│   ├── management/          # Custom commands
│   ├── migrations/          # Database migrations
│   ├── admin.py             # Admin configuration
│   ├── context_processors.py# Context processors for templates
│   ├── forms.py             # Forms
│   ├── middleware.py        # Custom middleware
│   ├── models.py            # Data models
│   ├── tmdb_client.py       # TMDB API client
│   ├── urls.py              # URL routes
│   └── views.py             # Views
├── movie_portal/            # Project config
│   ├── settings.py          # Django settings
│   ├── urls.py              # Project URLs
│   └── wsgi.py              # WSGI config
├── templates/               # Templates
│   ├── core/                # Core app templates
│   └── base.html            # Base template
├── static/                  # Static files
├── test/                    # Unused scripts and test files
├── manage.py                # Django manage script
├── db_config.py             # Database config
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

## Installation

### Prerequisites

- Python 3.8 or higher
- pip package manager
- Virtual environment (recommended)

### Setup

1. **Clone or download the project**

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   ```

3. **Activate the virtual environment**
   - On Windows:
     ```bash
     .\venv\Scripts\activate
     ```
   - On macOS/Linux:
     ```bash
     source venv/bin/activate
     ```

4. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

5. **Set up credentials**
   Create a `credentials.json` file in the project root:
   ```json
   {
     "TMDB_API_KEY": "your_tmdb_api_key_here",
     "CODESPECTERS_API_KEY": "your_codespecters_api_key_here"
   }
   ```

   Alternatively, you can set environment variables:
   - `TMDB_API_KEY`
   - `CODESPECTERS_API_KEY`
   - `DJANGO_SECRET_KEY` (recommended for production)
   - `DJANGO_DEBUG` (set to `True` for development, `False` for production)
   - `DJANGO_ALLOWED_HOSTS` (comma-separated list for production)

6. **Set up TMDB Database credentials (optional)**
   If you want to use the extracted TMDB PostgreSQL database, create a `cred` directory with a `tmdb_credentials.py` file:
   ```python
   import os

   DB_HOST = os.getenv("DB_HOST", "localhost")
   DB_PORT = os.getenv("DB_PORT", "5432")
   DB_NAME = os.getenv("DB_NAME", "tmdb")
   DB_USER = os.getenv("DB_USER", "tmdb")
   DB_PASSWORD = os.getenv("DB_PASSWORD", "tmdb123!")
   ```
   
   We've provided an example file at `cred/tmdb_credentials.example.py`. The `cred` directory is already in `.gitignore` to keep your credentials secure.

   You can also set these credentials via environment variables:
   - `DB_HOST`
   - `DB_PORT`
   - `DB_NAME`
   - `DB_USER`
   - `DB_PASSWORD`

7. **Apply database migrations**
   ```bash
   python manage.py migrate
   ```

8. **Create a superuser (for admin access)**
   ```bash
   python manage.py createsuperuser
   ```

9. **Run the development server**
   ```bash
   python manage.py runserver
   ```

10. **Open the application**
    Visit `http://127.0.0.1:8000` in your browser.

## Usage

### Data Sources

The application supports multiple data sources for movie and TV show information:

1. **TMDB API (default)**: Fetch real-time data from The Movie Database API
2. **TMDB Database (Extracted)**: Use a pre-extracted PostgreSQL database of TMDB data
3. **Local Database**: Use the local Django database populated with TMDB data
4. **Xtream**: Use an Xtream Codes API (for IPTV content)

To switch data sources, go to the Admin Dashboard > Site Settings > Data Source.

### Admin Dashboard

Access the admin dashboard at `http://127.0.0.1:8000/admin-dashboard/` (requires superuser or staff account).

- Manage site settings
- Configure content rows for the homepage
- Set up email credentials for password reset

### Reset Admin Password

If you need to reset the admin password, use the provided script:
```bash
python reset_admin_password.py
```

## Production Deployment

### Environment Variables

For production, set the following environment variables:

| Variable               | Description                                                                 |
|------------------------|-----------------------------------------------------------------------------|
| `DJANGO_SECRET_KEY`    | A secure secret key for Django                                              |
| `DJANGO_DEBUG`         | Set to `False` for production                                               |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list of allowed hosts (e.g., `yourdomain.com,www.yourdomain.com`) |
| `TMDB_API_KEY`         | Your TMDB API key                                                           |
| `CODESPECTERS_API_KEY` | Your CodeSpecters API key                                                   |

### Static Files

Collect static files for production:
```bash
python manage.py collectstatic
```

### Security

Production settings include:
- SSL redirect
- Secure cookies (session, CSRF)
- XSS and content type sniffing protection
- Clickjacking prevention (X-Frame-Options: DENY)

## License

This project is for educational and personal use only.

## Acknowledgements

- [The Movie Database (TMDB)](https://www.themoviedb.org/) for movie and TV show data
- [Bootstrap](https://getbootstrap.com/) for the UI framework
