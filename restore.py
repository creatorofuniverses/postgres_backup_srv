#!/usr/bin/env python3

import os
import sys
import yaml
import boto3
import logging
import subprocess
import tempfile
import argparse
import datetime
from pathlib import Path
from botocore.exceptions import ClientError
from s3_utils import create_s3_client, download_file

# Configure logging to file and console
log_dir = "/var/log/postgres-backup"
os.makedirs(log_dir, exist_ok=True)

# Create a unique log filename with timestamp
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = f"{log_dir}/restore_{timestamp}.log"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('postgres-restore')

logger.info(f"Restore script started. Log file: {log_file}")
logger.info(f"Command line arguments: {sys.argv}")

def load_config():
    """Load configuration from config.yaml file"""
    config_path = Path(__file__).parent / 'config.yaml'
    try:
        with open(config_path, 'r') as config_file:
            return yaml.safe_load(config_file)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)


def get_latest_backup(db_name):
    """Get the latest backup file for a database from S3"""
    config = load_config()
    s3_config = config['s3']
    
    s3_client = create_s3_client(s3_config)
    
    # Determine S3 prefix
    prefix = s3_config.get('prefix', '')
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    s3_prefix = f"{prefix}{db_name}/"
    
    try:
        # List objects in the bucket with the specified prefix
        response = s3_client.list_objects_v2(
            Bucket=s3_config['bucket'],
            Prefix=s3_prefix
        )
        
        if 'Contents' not in response:
            logger.error(f"No backups found for {db_name}")
            return None
        
        # Sort by last modified timestamp (newest first)
        backups = sorted(
            response['Contents'],
            key=lambda x: x['LastModified'],
            reverse=True
        )
        
        if not backups:
            logger.error(f"No backups found for {db_name}")
            return None
        
        latest_backup = backups[0]['Key']
        logger.info(f"Latest backup for {db_name}: {latest_backup}")
        return latest_backup
    
    except ClientError as e:
        logger.error(f"Failed to list backups: {e}")
        return None

def restore_database(db_name, backup_key=None):
    """Restore a database from S3 backup"""
    config = load_config()
    
    # Find the database configuration
    db_config = None
    for db in config.get('databases', []):
        if db['name'] == db_name:
            db_config = db
            break
    
    if not db_config:
        logger.error(f"Database {db_name} not found in configuration")
        return False
    
    # Get backup key if not provided
    if not backup_key:
        backup_key = get_latest_backup(db_name)
        if not backup_key:
            return False
    
    logger.info(f"Restoring database {db_name} from backup {backup_key}")
    
    # Ensure backup_key has the correct format
    s3_config = config['s3']
    prefix = s3_config.get('prefix', '')
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    
    # If backup_key doesn't contain the prefix and db_name, add them
    if not backup_key.startswith(prefix):
        # This might be just the filename, so construct the full path
        if '/' in backup_key:
            # This is already a path, but might not have the prefix
            backup_filename = os.path.basename(backup_key)
            backup_key = f"{prefix}{db_name}/{backup_filename}"
        else:
            # This is just the filename
            backup_key = f"{prefix}{db_name}/{backup_key}"
    
    logger.info(f"Using backup key: {backup_key}")
    
    # Download backup from S3
    s3_client = boto3.client(
        's3',
        region_name=s3_config['region'],
        aws_access_key_id=s3_config['access_key'],
        aws_secret_access_key=s3_config['secret_key'],
        endpoint_url=s3_config.get('endpoint_url')
    )
    
    # Create temporary file for the backup
    temp_file = tempfile.NamedTemporaryFile(delete=False)
    temp_file.close()
    
    try:
        # Download backup file
        logger.info(f"Downloading backup from S3: {s3_config['bucket']}/{backup_key}")
        download_file(s3_client, s3_config['bucket'], backup_key, temp_file.name)
        
        # Set environment variables for pg_restore
        env = os.environ.copy()
        env['PGPASSWORD'] = db_config['password']
        
        host = db_config['host']
        port = db_config.get('port', 5432)
        user = db_config['user']
        database = db_config['database']
        vector_extension = db_config.get('vector_extension', False)
        
        # Check if database exists and create it if it doesn't
        check_cmd = [
            'psql',
            '-h', host,
            '-p', str(port),
            '-U', user,
            '-lqt'
        ]
        
        process = subprocess.run(
            check_cmd,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        db_exists = False
        for line in process.stdout.splitlines():
            if database in line:
                db_exists = True
                break
        
        if not db_exists:
            logger.info(f"Database {database} does not exist, creating it")
            create_cmd = [
                'createdb',
                '-h', host,
                '-p', str(port),
                '-U', user,
                database
            ]
            
            subprocess.run(
                create_cmd,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        
        # If vector extension is used, create it before restore
        if vector_extension:
            logger.info(f"Creating vector extension for {database}")
            vector_cmd = [
                'psql',
                '-h', host,
                '-p', str(port),
                '-U', user,
                '-d', database,
                '-c', 'CREATE EXTENSION IF NOT EXISTS vector;'
            ]
            
            subprocess.run(
                vector_cmd,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        
        # Execute pg_restore
        restore_cmd = [
            'pg_restore',
            '-h', host,
            '-p', str(port),
            '-U', user,
            '-d', database,
            '--clean',
            '--if-exists',
            temp_file.name
        ]
        
        process = subprocess.run(
            restore_cmd,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        logger.info(f"Database {db_name} restored successfully")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Restore failed: {e.stderr.decode('utf-8')}")
        return False
    except ClientError as e:
        logger.error(f"S3 download failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        return False
    finally:
        # Clean up temporary file
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)

def list_backups(db_name):
    """List all available backups for a database"""
    config = load_config()
    s3_config = config['s3']
    
    s3_client = create_s3_client(s3_config)
    
    # Determine S3 prefix
    prefix = s3_config.get('prefix', '')
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    s3_prefix = f"{prefix}{db_name}/"
    
    try:
        # Use pagination to handle more than 1000 backups
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(
            Bucket=s3_config['bucket'],
            Prefix=s3_prefix
        )
        
        backups = []
        for page in pages:
            if 'Contents' in page:
                backups.extend(page['Contents'])
        
        # Sort by last modified timestamp (newest first)
        backups = sorted(
            backups,
            key=lambda x: x['LastModified'],
            reverse=True
        )
        
        if not backups:
            logger.info(f"No backups found for {db_name}")
            return
        
        logger.info(f"Available backups for {db_name}:")
        for i, backup in enumerate(backups, 1):
            logger.info(f"{i}. {os.path.basename(backup['Key'])} - {backup['LastModified']}")
            
        return backups
    
    except ClientError as e:
        logger.error(f"Failed to list backups: {e}")
        return None

def main():
    """Main function to run the restore script"""
    parser = argparse.ArgumentParser(description='Restore PostgreSQL database from S3 backup')
    parser.add_argument('db_name', help='Name of the database to restore')
    parser.add_argument('--list', action='store_true', help='List available backups')
    parser.add_argument('--backup-key', help='Specific backup key to restore')
    parser.add_argument('--backup-number', type=int, help='Backup number from the list to restore')
    
    args = parser.parse_args()
    logger.info(f"Parsed arguments: {args}")
    
    if args.list:
        logger.info(f"Listing backups for database: {args.db_name}")
        backups = list_backups(args.db_name)
        return
    
    if args.backup_number:
        logger.info(f"Restoring database {args.db_name} using backup number: {args.backup_number}")
        backups = list_backups(args.db_name)
        if not backups or args.backup_number > len(backups):
            logger.error(f"Invalid backup number: {args.backup_number}")
            return
        
        backup_key = backups[args.backup_number - 1]['Key']
        logger.info(f"Selected backup key: {backup_key}")
        success = restore_database(args.db_name, backup_key)
        sys.exit(0 if success else 1)
    elif args.backup_key:
        logger.info(f"Restoring database {args.db_name} using backup key: {args.backup_key}")
        success = restore_database(args.db_name, args.backup_key)
        sys.exit(0 if success else 1)
    else:
        logger.info(f"Restoring database {args.db_name} using latest backup")
        success = restore_database(args.db_name)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"Unhandled exception in restore script: {e}")
        sys.exit(1)