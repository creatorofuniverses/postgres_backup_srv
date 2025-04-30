FROM python:3.11-slim

# Add PostgreSQL 17 repository
RUN apt-get update && apt-get install -y lsb-release gnupg2 wget
RUN echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list
RUN wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | apt-key add -

# Install PostgreSQL 17 client tools and other dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client-17 \
    cron \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY backup.py restore.py web_ui.py s3_utils.py config.yaml ./
COPY templates/ ./templates/
COPY static/ ./static/

# Create directory for logs
RUN mkdir -p /var/log/postgres-backup

# Make scripts executable
RUN chmod +x backup.py restore.py web_ui.py

# Set up cron job for scheduled backups with PYTHONPATH
RUN echo "PYTHONPATH=/app\n0 2 * * * /usr/local/bin/python /app/backup.py >> /var/log/postgres-backup/backup.log 2>&1" > /etc/cron.d/postgres-backup
RUN chmod 0644 /etc/cron.d/postgres-backup
RUN crontab /etc/cron.d/postgres-backup

# Create supervisord configuration
RUN echo '[supervisord]\n\
nodaemon=true\n\
logfile=/var/log/supervisor/supervisord.log\n\
logfile_maxbytes=10MB\n\
logfile_backups=5\n\
\n\
[program:cron]\n\
command=cron -f\n\
autostart=true\n\
autorestart=true\n\
environment=PYTHONPATH="/app"\n\
\n\
[program:web_ui]\n\
command=python /app/web_ui.py\n\
autostart=true\n\
autorestart=true\n\
environment=PYTHONPATH="/app"\n\
stdout_logfile=/var/log/postgres-backup/web_ui.log\n\
stderr_logfile=/var/log/postgres-backup/web_ui_error.log\n\
\n\
[program:initial_backup]\n\
command=python /app/backup.py\n\
autostart=true\n\
autorestart=false\n\
startsecs=0\n\
environment=PYTHONPATH="/app"\n\
stdout_logfile=/var/log/postgres-backup/backup.log\n\
stderr_logfile=/var/log/postgres-backup/backup_error.log\n\
' > /etc/supervisor/conf.d/supervisord.conf

# Create log directories for supervisor
RUN mkdir -p /var/log/supervisor

# Expose port for web UI
EXPOSE 5000

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]