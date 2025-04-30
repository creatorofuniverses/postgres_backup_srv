import os
import boto3
import logging
import random
import time
from botocore.exceptions import ClientError


logger = logging.getLogger('s3-utils')

def create_s3_client(s3_config):
    """Create an S3 client with appropriate configuration"""
    # Extract configuration
    region = s3_config.get('region')
    access_key = s3_config.get('access_key')
    secret_key = s3_config.get('secret_key')
    endpoint_url = s3_config.get('endpoint_url')
    use_path_style = s3_config.get('use_path_style', False)
    use_iam_role = s3_config.get('use_iam_role', False)
    
    # Create client config
    client_config = boto3.session.Config(
        signature_version='s3v4',
        s3={
            'addressing_style': 'path' if use_path_style else 'virtual',
            'use_accelerate_endpoint': False
        },
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required"
    )
    
    # Create client with appropriate parameters
    client_kwargs = {
        'service_name': 's3',
        'region_name': region,
        'config': client_config
    }
    
    # Only add credentials if not using IAM role
    if not use_iam_role:
        client_kwargs['aws_access_key_id'] = access_key
        client_kwargs['aws_secret_access_key'] = secret_key
    
    # Only add endpoint_url if it's not None/null
    if endpoint_url:
        client_kwargs['endpoint_url'] = endpoint_url
    
    return boto3.client(**client_kwargs)

def upload_file(s3_client, file_path, bucket, key, max_retries=5):
    """Upload a file to S3 with retry logic"""
    for attempt in range(max_retries):
        try:
            logger.info(f"Upload attempt {attempt+1}/{max_retries}: {bucket}/{key}")
            
            # For large files, use multipart upload
            file_size = os.path.getsize(file_path)
            if file_size > 10 * 1024 * 1024:  # 10 MB
                # Use multipart upload for large files
                transfer_config = boto3.s3.transfer.TransferConfig(
                    multipart_threshold=8 * 1024 * 1024,  # 8 MB
                    max_concurrency=10,
                    multipart_chunksize=8 * 1024 * 1024,  # 8 MB
                    use_threads=True
                )
                transfer = boto3.s3.transfer.S3Transfer(
                    client=s3_client,
                    config=transfer_config
                )
                transfer.upload_file(file_path, bucket, key)
            else:
                # Use regular upload for smaller files
                s3_client.upload_file(file_path, bucket, key)
                
            logger.info(f"Upload successful: {bucket}/{key}")
            return True
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            logger.error(f"Upload attempt {attempt+1} failed with error code {error_code}: {str(e)}")
            
            # Determine if we should retry based on the error
            if error_code in ['RequestTimeout', 'RequestTimeTooSkewed', 
                             'ProvisionedThroughputExceededException', 
                             'SlowDown', 'InternalError', 'ServiceUnavailable']:
                if attempt < max_retries - 1:
                    # Calculate backoff time (exponential with jitter)
                    backoff_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.info(f"Retrying in {backoff_time:.2f} seconds...")
                    time.sleep(backoff_time)
                else:
                    logger.error(f"Max retries reached. Upload failed: {str(e)}")
                    return False
            else:
                # Non-retryable error
                logger.error(f"Non-retryable error. Upload failed: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"Unexpected error during upload: {str(e)}")
            if attempt < max_retries - 1:
                backoff_time = (2 ** attempt) + random.uniform(0, 1)
                logger.info(f"Retrying in {backoff_time:.2f} seconds...")
                time.sleep(backoff_time)
            else:
                logger.error(f"Max retries reached. Upload failed: {str(e)}")
                return False
                
    return False

def download_file(s3_client, bucket, key, file_path, max_retries=5):
    """Download a file from S3 with retry logic"""
    for attempt in range(max_retries):
        try:
            logger.info(f"Download attempt {attempt+1}/{max_retries}: {bucket}/{key}")
            s3_client.download_file(bucket, key, file_path)
            logger.info(f"Download successful: {bucket}/{key}")
            return True
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            logger.error(f"Download attempt {attempt+1} failed with error code {error_code}: {str(e)}")
            
            # Determine if we should retry based on the error
            if error_code in ['RequestTimeout', 'RequestTimeTooSkewed', 
                             'ProvisionedThroughputExceededException', 
                             'SlowDown', 'InternalError', 'ServiceUnavailable']:
                if attempt < max_retries - 1:
                    # Calculate backoff time (exponential with jitter)
                    backoff_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.info(f"Retrying in {backoff_time:.2f} seconds...")
                    time.sleep(backoff_time)
                else:
                    logger.error(f"Max retries reached. Download failed: {str(e)}")
                    return False
            else:
                # Non-retryable error
                logger.error(f"Non-retryable error. Download failed: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"Unexpected error during download: {str(e)}")
            if attempt < max_retries - 1:
                backoff_time = (2 ** attempt) + random.uniform(0, 1)
                logger.info(f"Retrying in {backoff_time:.2f} seconds...")
                time.sleep(backoff_time)
            else:
                logger.error(f"Max retries reached. Download failed: {str(e)}")
                return False
                
    return False