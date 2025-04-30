#!/usr/bin/env python3

import os
import sys
import yaml
import boto3
import logging
import subprocess
import tempfile
import datetime
import schedule
import time
from pathlib import Path
from botocore.exceptions import ClientError
# Import the utility functions
from s3_utils import create_s3_client, upload_file

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('postgres-backup')

def load_config():
    """Load configuration from config.yaml file"""
    config_path = Path(__file__).parent / 'config.yaml'
    try:
        with open(config_path, 'r') as config_file:
            return yaml.safe_load(config_file)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)

def backup_database(db_config):
    """Backup a single database to S3"""
    db_name = db_config['name']
    host = db_config['host']
    port = db_config.get('port', 5432)
    user = db_config['user']
    password = db_config['password']
    database = db_config['database']
    vector_extension = db_config.get('vector_extension', False)
    
    logger.info(f"Starting backup for database {db_name} ({database})")
    
    # Create a timestamp for the backup filename
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup_filename = f"{db_name}_{timestamp}.dump"
    
    # Create temporary file for the backup
    temp_file = tempfile.NamedTemporaryFile(delete=False)
    temp_file.close()
    
    # Set environment variables for pg_dump
    env = os.environ.copy()
    env['PGPASSWORD'] = password
    
    try:
        # Execute pg_dump
        cmd = [
            'pg_dump',
            '-h', host,
            '-p', str(port),
            '-U', user,
            '-d', database,
            '-F', 'c',  # Custom format
            '-f', temp_file.name
        ]
        
        # Add extra options for vector extension if needed
        if vector_extension:
            cmd.extend(['--no-owner', '--no-acl'])
        
        process = subprocess.run(
            cmd,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        logger.info(f"Database dump completed successfully for {db_name}")
        
        # Upload to S3
        config = load_config()
        s3_config = config['s3']
        
        s3_client = create_s3_client(s3_config)
        
        # Determine S3 path
        prefix = s3_config.get('prefix', '')
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        
        s3_path = f"{prefix}{db_name}/{backup_filename}"
        
        logger.info(f"Uploading backup to S3: {s3_config['bucket']}/{s3_path}")

        upload_file(s3_client, temp_file.name, s3_config['bucket'], s3_path)
        
        logger.info(f"Backup successfully uploaded to S3 for {db_name}")
        
        # Clean up old backups if retention is configured
        cleanup_old_backups(db_name)
        
    except subprocess.CalledProcessError as e:
        logger.error(f"pg_dump failed for {db_name}: {e.stderr.decode('utf-8')}")
    except ClientError as e:
        logger.error(f"S3 upload failed for {db_name}: {e}")
    except Exception as e:
        logger.error(f"Backup failed for {db_name}: {e}")
    finally:
        # Clean up temporary file
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)

def cleanup_old_backups(db_name):
    """Remove backups older than retention period"""
    config = load_config()
    retention_days = config.get('retention', {}).get('days', 7)
    
    if retention_days <= 0:
        logger.info("Retention policy disabled, skipping cleanup")
        return
    
    logger.info(f"Cleaning up backups older than {retention_days} days for {db_name}")
    
    s3_config = config['s3']
    s3_client = create_s3_client(s3_config)
    # Calculate cutoff date
    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    
    # Determine S3 prefix
    prefix = s3_config.get('prefix', '')
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    s3_prefix = f"{prefix}{db_name}/"
    
    try:
        # List objects in the bucket with the specified prefix
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(
            Bucket=s3_config['bucket'],
            Prefix=s3_prefix
        )
        
        objects_to_delete = []
        
        for page in pages:
            if 'Contents' not in page:
                continue
                
            for obj in page['Contents']:
                # Extract timestamp from filename
                filename = os.path.basename(obj['Key'])
                try:
                    # Parse timestamp from filename (format: dbname_YYYY-MM-DD_HH-MM-SS.dump)
                    parts = filename.split('_')
                    if len(parts) >= 3:
                        date_part = parts[-2]
                        time_part = parts[-1].split('.')[0]
                        timestamp_str = f"{date_part}_{time_part}"
                        timestamp = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d_%H-%M-%S')
                        
                        if timestamp < cutoff_date:
                            objects_to_delete.append({'Key': obj['Key']})
                except Exception as e:
                    logger.warning(f"Failed to parse timestamp from {filename}: {e}")
        
        # Delete old backups
        if objects_to_delete:
            s3_client.delete_objects(
                Bucket=s3_config['bucket'],
                Delete={'Objects': objects_to_delete}
            )
            logger.info(f"Deleted {len(objects_to_delete)} old backups for {db_name}")
        else:
            logger.info(f"No old backups to delete for {db_name}")
            
    except ClientError as e:
        logger.error(f"Failed to clean up old backups: {e}")

def backup_all_databases():
    """Backup all databases defined in config"""
    config = load_config()
    for db_config in config.get('databases', []):
        backup_database(db_config)

def setup_schedules():
    """Set up scheduled backups based on config"""
    config = load_config()
    
    # First run an immediate backup for all databases
    backup_all_databases()
    
    # Then set up scheduled backups
    for db_config in config.get('databases', []):
        if 'schedule' in db_config:
            db_name = db_config['name']
            cron_schedule = db_config['schedule']
            
            logger.info(f"Setting up scheduled backup for {db_name} with schedule: {cron_schedule}")
            
            # Parse cron schedule to schedule format
            parts = cron_schedule.split()
            if len(parts) >= 5:
                minute, hour, day_of_month, month, day_of_week = parts[:5]
                
                # Convert to schedule format
                if minute == '*':
                    minute = '0-59'
                if hour == '*':
                    hour = '0-23'
                if day_of_month == '*':
                    day_of_month = '1-31'
                if month == '*':
                    month = '1-12'
                if day_of_week == '*':
                    day_of_week = '0-6'
                
                # Schedule backup job
                if minute != '*' and hour != '*':
                    schedule_str = f"at {hour.zfill(2)}:{minute.zfill(2)}"
                    if day_of_week != '*':
                        days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                        day_names = []
                        for i in day_of_week.split(','):
                            if '-' in i:
                                start, end = map(int, i.split('-'))
                                day_names.extend(days[d] for d in range(start, end+1))
                            else:
                                day_names.append(days[int(i)])
                        
                        for day in day_names:
                            getattr(schedule.every(), day).at(f"{hour.zfill(2)}:{minute.zfill(2)}").do(
                                lambda db=db_config: backup_database(db)
                            )
                    else:
                        schedule.every().day.at(f"{hour.zfill(2)}:{minute.zfill(2)}").do(
                            lambda db=db_config: backup_database(db)
                        )
            else:
                logger.warning(f"Invalid cron schedule for {db_name}: {cron_schedule}")

def main():
    """Main function to run the backup service"""
    logger.info("Starting PostgreSQL backup service")
    
    # Set up scheduled backups
    setup_schedules()
    
    # Run the scheduler
    logger.info("Scheduler started, waiting for jobs...")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # If a database name is provided, backup only that database
        db_name = sys.argv[1]
        config = load_config()
        
        # Find the database configuration
        db_config = None
        for db in config.get('databases', []):
            if db['name'] == db_name:
                db_config = db
                break
        
        if db_config:
            backup_database(db_config)
        else:
            logger.error(f"Database {db_name} not found in configuration")
    else:
        # Otherwise backup all databases
        main()