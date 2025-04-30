#!/usr/bin/env python3

import os
import yaml
import boto3
import logging
import datetime
import humanize
import subprocess
import tempfile
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_from_directory
from botocore.exceptions import ClientError

# Import s3_utils
try:
    from s3_utils import create_s3_client, download_file
except ImportError:
    # Fallback implementation
    def create_s3_client(s3_config):
        client_kwargs = {
            'service_name': 's3',
            'region_name': s3_config.get('region'),
            'aws_access_key_id': s3_config.get('access_key'),
            'aws_secret_access_key': s3_config.get('secret_key')
        }
        if s3_config.get('endpoint_url'):
            client_kwargs['endpoint_url'] = s3_config.get('endpoint_url')
        return boto3.client(**client_kwargs)
    
    def download_file(s3_client, bucket, key, file_path, max_retries=3):
        try:
            s3_client.download_file(bucket, key, file_path)
            return True
        except Exception as e:
            logging.error(f"Download failed: {e}")
            return False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('backup-monitor')

app = Flask(__name__, 
            static_folder='static',
            template_folder='templates')

# Add a route for favicon.ico
@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

def load_config():
    """Load configuration from config.yaml file"""
    config_path = Path(__file__).parent / 'config.yaml'
    try:
        with open(config_path, 'r') as config_file:
            return yaml.safe_load(config_file)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}

def get_s3_client():
    """Create and return an S3 client"""
    config = load_config()
    s3_config = config.get('s3', {})
    return create_s3_client(s3_config)

def get_backup_info():
    """Get information about all backups"""
    config = load_config()
    s3_config = config.get('s3', {})
    databases = config.get('databases', [])
    
    result = {
        'databases': [],
        'last_check': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        's3_bucket': s3_config.get('bucket', 'N/A'),
        's3_region': s3_config.get('region', 'N/A'),
        's3_prefix': s3_config.get('prefix', ''),
        'retention_days': config.get('retention', {}).get('days', 'N/A')
    }
    
    s3_client = get_s3_client()
    
    for db in databases:
        db_name = db.get('name', 'unknown')
        db_host = db.get('host', 'unknown')
        db_database = db.get('database', 'unknown')
        db_schedule = db.get('schedule', 'N/A')
        vector_extension = db.get('vector_extension', False)
        
        # Determine S3 prefix
        prefix = s3_config.get('prefix', '')
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        s3_prefix = f"{prefix}{db_name}/"
        
        db_info = {
            'name': db_name,
            'host': db_host,
            'database': db_database,
            'schedule': db_schedule,
            'vector_extension': 'Yes' if vector_extension else 'No',
            'last_backup': 'Never',
            'last_backup_age': 'N/A',
            'last_backup_size': 'N/A',
            'total_backups': 0,
            'total_size': 0,
            'backups': []
        }
        
        try:
            # List objects in the bucket with the specified prefix
            paginator = s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(
                Bucket=s3_config.get('bucket', ''),
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
            
            total_size = sum(b['Size'] for b in backups)
            db_info['total_backups'] = len(backups)
            db_info['total_size'] = humanize.naturalsize(total_size)
            
            # Add information about all backups (not just the last 5)
            for backup in backups:
                backup_name = os.path.basename(backup['Key'])
                backup_time = backup['LastModified']
                backup_size = backup['Size']
                backup_age = datetime.datetime.now(datetime.timezone.utc) - backup_time
                
                backup_info = {
                    'name': backup_name,
                    'time': backup_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'size': humanize.naturalsize(backup_size),
                    'age': humanize.naturaltime(backup_age)
                }
                
                db_info['backups'].append(backup_info)
            
            # Update last backup info
            if backups:
                last_backup = backups[0]
                last_backup_time = last_backup['LastModified']
                last_backup_age = datetime.datetime.now(datetime.timezone.utc) - last_backup_time
                
                db_info['last_backup'] = last_backup_time.strftime('%Y-%m-%d %H:%M:%S')
                db_info['last_backup_age'] = humanize.naturaltime(last_backup_age)
                db_info['last_backup_size'] = humanize.naturalsize(last_backup['Size'])
        
        except ClientError as e:
            logger.error(f"Failed to list backups for {db_name}: {e}")
            db_info['error'] = str(e)
        
        result['databases'].append(db_info)
    
    return result

@app.route('/')
def index():
    """Render the main dashboard page"""
    return render_template('index.html')

@app.route('/api/backups')
def api_backups():
    """API endpoint to get backup information"""
    return jsonify(get_backup_info())

@app.route('/api/backup', methods=['POST'])
def api_backup_all():
    """API endpoint to trigger backup for all databases"""
    try:
        # Run the backup script
        process = subprocess.run(
            ['python', '/app/backup.py'],
            capture_output=True,
            text=True,
            check=True
        )
        
        return jsonify({
            'success': True,
            'message': 'All databases backed up successfully'
        })
    except subprocess.CalledProcessError as e:
        logger.error(f"Backup failed: {e.stderr}")
        return jsonify({
            'success': False,
            'message': f"Backup failed: {e.stderr}"
        })
    except Exception as e:
        logger.error(f"Backup failed: {str(e)}")
        return jsonify({
            'success': False,
            'message': f"Backup failed: {str(e)}"
        })

@app.route('/api/backup/<db_name>', methods=['POST'])
def api_backup_db(db_name):
    """API endpoint to trigger backup for a specific database"""
    try:
        # Run the backup script for the specific database
        process = subprocess.run(
            ['python', '/app/backup.py', db_name],
            capture_output=True,
            text=True,
            check=True
        )
        
        return jsonify({
            'success': True,
            'message': f'Database {db_name} backed up successfully'
        })
    except subprocess.CalledProcessError as e:
        logger.error(f"Backup failed for {db_name}: {e.stderr}")
        return jsonify({
            'success': False,
            'message': f"Backup failed for {db_name}: {e.stderr}"
        })
    except Exception as e:
        logger.error(f"Backup failed for {db_name}: {str(e)}")
        return jsonify({
            'success': False,
            'message': f"Backup failed for {db_name}: {str(e)}"
        })

@app.route('/api/restore/<db_name>', methods=['POST'])
def api_restore_db(db_name):
    """API endpoint to restore a database from a backup"""
    data = request.json
    backup_name = data.get('backup_name')
    
    if not backup_name:
        return jsonify({
            'success': False,
            'message': 'Backup name is required'
        })
    
    logger.info(f"Restore request received for database {db_name}, backup {backup_name}")
    
    try:
        # Get the full S3 path for the backup
        config = load_config()
        s3_config = config.get('s3', {})
        prefix = s3_config.get('prefix', '')
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        
        # Construct the full backup key
        full_backup_key = f"{prefix}{db_name}/{backup_name}"
        logger.info(f"Constructed full backup key: {full_backup_key}")
        
        # Run the restore script with the full backup key
        cmd = [
            'python', 
            '/app/restore.py', 
            db_name, 
            '--backup-key', 
            full_backup_key  # Use the full backup key here
        ]
        
        logger.info(f"Executing command: {' '.join(cmd)}")
        
        # Run the command
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )
        
        # Log the output regardless of success/failure
        logger.info(f"Command exit code: {process.returncode}")
        logger.info(f"Command stdout: {process.stdout}")
        logger.info(f"Command stderr: {process.stderr}")
        
        # Check restore logs
        restore_logs = []
        log_dir = "/var/log/postgres-backup"
        for filename in os.listdir(log_dir):
            if filename.startswith(f"restore_") and filename.endswith(".log"):
                log_path = os.path.join(log_dir, filename)
                file_stat = os.stat(log_path)
                # Only check recent logs (less than 5 minutes old)
                if datetime.datetime.fromtimestamp(file_stat.st_mtime) > datetime.datetime.now() - datetime.timedelta(minutes=5):
                    with open(log_path, 'r') as f:
                        log_content = f.read()
                        restore_logs.append({
                            'filename': filename,
                            'content': log_content
                        })
        
        if process.returncode == 0:
            return jsonify({
                'success': True,
                'message': f'Database {db_name} restored successfully from {backup_name}',
                'logs': restore_logs
            })
        else:
            return jsonify({
                'success': False,
                'message': f"Restore failed for {db_name}. Exit code: {process.returncode}",
                'stdout': process.stdout,
                'stderr': process.stderr,
                'logs': restore_logs
            })
    except Exception as e:
        logger.exception(f"Error in restore API: {e}")
        return jsonify({
            'success': False,
            'message': f"Restore failed for {db_name}: {str(e)}"
        })


@app.route('/api/restore-status/<db_name>')
def restore_status(db_name):
    """Get the status of a restore operation"""
    # Check if a restore is in progress
    restore_in_progress = False
    restore_message = ""
    restore_progress = 0
    
    # Check for recent restore logs
    log_dir = "/var/log/postgres-backup"
    recent_logs = []
    
    try:
        for filename in os.listdir(log_dir):
            if filename.startswith(f"restore_{db_name}_") and filename.endswith(".log"):
                log_path = os.path.join(log_dir, filename)
                file_stat = os.stat(log_path)
                # Only check recent logs (less than 5 minutes old)
                if datetime.datetime.fromtimestamp(file_stat.st_mtime) > datetime.datetime.now() - datetime.timedelta(minutes=5):
                    recent_logs.append({
                        'path': log_path,
                        'mtime': file_stat.st_mtime
                    })
        
        # Sort by modification time (newest first)
        recent_logs.sort(key=lambda x: x['mtime'], reverse=True)
        
        if recent_logs:
            # Check the most recent log
            latest_log = recent_logs[0]
            with open(latest_log['path'], 'r') as f:
                log_content = f.read()
                
                # Check if the restore is still in progress
                if "Restore script started" in log_content and "Restore complete" not in log_content:
                    restore_in_progress = True
                    restore_message = "Restore in progress..."
                    
                    # Try to estimate progress
                    if "Downloading backup from S3" in log_content:
                        restore_progress = 30
                    elif "Creating vector extension" in log_content:
                        restore_progress = 50
                    elif "Executing pg_restore" in log_content:
                        restore_progress = 70
                    elif "Database restored successfully" in log_content:
                        restore_in_progress = False
                        restore_message = "Restore completed successfully"
                        restore_progress = 100
    except Exception as e:
        logger.error(f"Error checking restore status: {e}")
    
    return jsonify({
        'in_progress': restore_in_progress,
        'message': restore_message,
        'progress': restore_progress
    })

@app.route('/api/test-restore/<db_name>/<backup_name>')
def test_restore(db_name, backup_name):
    """Test endpoint to debug restore issues"""
    results = {
        'db_name': db_name,
        'backup_name': backup_name,
        'tests': []
    }
    
    # Test 1: Check if restore.py exists
    test1 = {
        'name': 'Check restore.py exists',
        'command': 'ls -l /app/restore.py',
        'result': None,
        'output': None,
        'success': False
    }
    
    try:
        process = subprocess.run(
            ['ls', '-l', '/app/restore.py'],
            capture_output=True,
            text=True,
            check=True
        )
        test1['result'] = 'Success'
        test1['output'] = process.stdout
        test1['success'] = True
    except Exception as e:
        test1['result'] = 'Failed'
        test1['output'] = str(e)
    
    results['tests'].append(test1)
    
    # Test 2: Run restore.py --help
    test2 = {
        'name': 'Check restore.py help',
        'command': 'python /app/restore.py --help',
        'result': None,
        'output': None,
        'success': False
    }
    
    try:
        process = subprocess.run(
            ['python', '/app/restore.py', '--help'],
            capture_output=True,
            text=True,
            check=True
        )
        test2['result'] = 'Success'
        test2['output'] = process.stdout
        test2['success'] = True
    except Exception as e:
        test2['result'] = 'Failed'
        test2['output'] = str(e)
    
    results['tests'].append(test2)
    
    # Test 3: List backups
    test3 = {
        'name': 'List backups',
        'command': f'python /app/restore.py {db_name} --list',
        'result': None,
        'output': None,
        'success': False
    }
    
    try:
        process = subprocess.run(
            ['python', '/app/restore.py', db_name, '--list'],
            capture_output=True,
            text=True,
            check=True
        )
        test3['result'] = 'Success'
        test3['output'] = process.stdout
        test3['success'] = True
    except Exception as e:
        test3['result'] = 'Failed'
        test3['output'] = str(e)
    
    results['tests'].append(test3)
    
    # Test 4: Try restore command
    test4 = {
        'name': 'Try restore command',
        'command': f'python /app/restore.py {db_name} --backup-key {backup_name}',
        'result': None,
        'output': None,
        'success': False
    }
    
    try:
        process = subprocess.run(
            ['python', '/app/restore.py', db_name, '--backup-key', backup_name],
            capture_output=True,
            text=True
        )
        test4['result'] = 'Exit code: ' + str(process.returncode)
        test4['output'] = f"STDOUT: {process.stdout}\nSTDERR: {process.stderr}"
        test4['success'] = process.returncode == 0
    except Exception as e:
        test4['result'] = 'Failed'
        test4['output'] = str(e)
    
    results['tests'].append(test4)
    
    return jsonify(results)

def main():
    """Main function to run the web UI"""
    app.run(host='0.0.0.0', port=5000)

if __name__ == "__main__":
    main()