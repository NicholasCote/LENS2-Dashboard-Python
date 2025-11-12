import os
import boto3
import logging
import sys
from botocore import UNSIGNED
from botocore.config import Config
import requests.packages.urllib3
requests.packages.urllib3.disable_warnings()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('stratus')

def stratus_s3_client():
    try:
        endpoint = "https://stratus.ucar.edu/"
        session = boto3.session.Session()
        logger.info(f"Creating S3 client with endpoint {endpoint}")
        
        # Create a minimal config to avoid sending unsupported headers
        s3_client = session.client(
            service_name='s3',
            endpoint_url=endpoint,
            config=Config(
                signature_version=UNSIGNED,
                s3={
                    'addressing_style': 'path',
                    'payload_signing_enabled': False,
                    'use_accelerate_endpoint': False,
                },
                retries={'max_attempts': 3},
                connect_timeout=60,
                read_timeout=60
            ),
            verify=False
        )
        return s3_client
    except Exception as e:
        logger.error(f"Failed to create S3 client: {str(e)}")
        raise

def stratus_s3_resource():
    try:
        endpoint = "https://stratus.ucar.edu/"
        session = boto3.session.Session()
        logger.info(f"Creating S3 resource with endpoint {endpoint}")
        s3_resource = session.resource(
            service_name='s3',
            endpoint_url=endpoint,
            config=Config(
                signature_version=UNSIGNED,
                s3={'addressing_style': 'path'}  # Use path-style addressing
            ),
            verify=False
        )
        return s3_resource
    except Exception as e:
        logger.error(f"Failed to create S3 resource: {str(e)}")
        raise

def list_bucket_objs(bucket):
    try:
        bucket_objs = []
        logger.info(f"Listing objects in bucket: {bucket}")
        s3_resource = stratus_s3_resource()
        bucket_resource = s3_resource.Bucket(bucket)
        
        count = 0
        for obj in bucket_resource.objects.all():
            bucket_objs.append(obj.key)
            count += 1
            if count % 100 == 0:
                logger.info(f"Listed {count} objects so far...")
        
        logger.info(f"Found {len(bucket_objs)} objects in bucket {bucket}")
        return bucket_objs
    except Exception as e:
        logger.error(f"Error listing bucket objects: {str(e)}")
        raise

def download_file(filename, bucketname):
    try:
        logger.info(f"Downloading file: {filename} from bucket: {bucketname}")
        s3_client = stratus_s3_client()
        
        if "/" in filename:
            directory_split = filename.split("/")
            directory_split.pop(len(directory_split)-1)
            directory = "/".join(directory_split)
            if not os.path.exists(directory):
                logger.info(f"Creating directory: {directory}")
                os.makedirs(directory, exist_ok=True)
        
        # Check if file already exists
        if os.path.exists(filename):
            logger.info(f"File already exists: {filename}, skipping download")
            return
            
        logger.info(f"Starting download of {filename}")
        s3_client.download_file(bucketname, filename, filename, Config=Config(signature_version=UNSIGNED))
        logger.info(f"Successfully downloaded: {filename}")
    except Exception as e:
        logger.error(f"Failed to download {filename}: {str(e)}")
        raise

def get_data_files():
    try:
        logger.info("Starting data file download process")
        bucket_objs = list_bucket_objs('cisl-cloud-users')
        
        file_count = 0
        for obj in bucket_objs:
            if 'LENS2-ncote-dashboard/data_files' in obj and not obj.endswith('.tar.gz'):
                logger.info(f"Processing file: {obj}")
                download_file(obj, 'cisl-cloud-users')
                file_count += 1
                
        logger.info(f"Download process completed. Downloaded {file_count} files.")
        return True
    except Exception as e:
        logger.error(f"Data download process failed: {str(e)}")
        raise