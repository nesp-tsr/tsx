from flask import Blueprint, jsonify, request, send_file
from nesp.util import next_path, local_iso_datetime
from nesp.api.util import db_session
from nesp.api.auth import get_user_id
from nesp.api.upload import get_upload_path, get_upload_name
from nesp.importer import Importer
from nesp.config import data_dir
import logging
import os
from multiprocessing import Pool, Manager
import json
import traceback
from shutil import rmtree

bp = Blueprint('data_import', __name__)

imports_path = data_dir("imports")
running_imports = {} # Holds information about running imports

# This is called in a separate process
def process_import(file_path, working_path, commit, shared_ns):
	try:
		# Create logger for this import
		log_file = os.path.join(working_path, 'import.log')
		log = logging.getLogger(working_path)
		handler = logging.FileHandler(log_file, mode='w')
		handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
		handler.setLevel(logging.INFO)
		log.setLevel(logging.INFO)
		log.addHandler(handler)

		# Plumbing to notify parent process of our progress
		def update_progress(processed_rows, total_rows):
			shared_ns.total_rows, shared_ns.processed_rows = total_rows, processed_rows

		importer = Importer(file_path, commit = commit, logger = log, progress_callback = update_progress)
		importer.ingest_data()

		return {
			'warnings': importer.warning_count,
			'errors': importer.error_count
		}
	except:
		traceback.print_exc()
		raise

@bp.route('/imports', methods = ['POST'])
def post_import():
	body = request.json

	# Check upload parameter
	if 'upload_uuid' not in body:
		return jsonify("upload_uuid is required"), 400

	upload_uuid = body['upload_uuid']
	file_path = get_upload_path(upload_uuid)

	if not os.path.exists(file_path):
		return jsonify("invalid upload_uuid"), 400

	if 'name' not in body:
		return jsonify("name is required"), 400
	name = body['name']

	# Create new working directory for the import
	working_path, import_id = next_path(os.path.join(imports_path, "%04d"))
	os.makedirs(working_path)

	save_import_info(import_id, {
		'id': import_id,
		'upload_uuid': body['upload_uuid'],
		'status': 'init',
		'filename': get_upload_name(upload_uuid),
		'name': name,
		'created': local_iso_datetime()
	})

	process_import_async(import_id, 'checking')

	# TODO - Ideally should return 201 Created and URL of new resource
	return jsonify(import_info(import_id))

def process_import_async(import_id, status):
	info = import_info(import_id)

	file_path = get_upload_path(info['upload_uuid'])
	working_path = import_path(import_id)

	# Setup interprocess communication with import process
	manager = Manager()
	shared_ns = manager.Namespace()
	shared_ns.total_rows = 0
	shared_ns.processed_rows = 0

	running_imports[import_id] = {
		'started': local_iso_datetime(),
		'status': status,
		'ns': shared_ns
	}

	def process_result(result):
		del running_imports[import_id]
		success = result['errors'] == 0

		if status == 'checking':
			new_status = 'checked_ok' if success else 'checked_error'
		elif status == 'importing':
			new_status = 'imported' if success else 'import_error'

		update_import_info(import_id, {
			'status': new_status,
			'errors': result['errors'],
			'warnings': result['warnings']
		})

	# Start import process
	import_processing_pool.apply_async(process_import, (file_path, working_path, status == 'importing', shared_ns), callback = process_result)

@bp.route('/imports/<int:id>', methods = ['PUT'])
def update_import(id=None):
	new_info = request.json
	info = import_info(id)

	if not info:
		return "Not found", 404

	new_status = new_info['status']
	old_status = info['status']

	# Make sure this is a valid state transition
	if (old_status, new_status) not in (('checked_ok', 'checking'), ('checked_ok', 'importing'), ('checked_error', 'checking'), ('import_error', 'checking')):
		return "Invalid status change %s -> %s" % (old_status, new_status), 400

	if new_status == 'importing' and info['upload_uuid'] != new_info['upload_uuid']:
		return "Attempting to import unchecked upload", 400

	update_import_info(id, {
		'upload_uuid': new_info['upload_uuid'],
		'name': new_info['name']
	})

	process_import_async(id, new_status)

	return jsonify(import_info(id))

@bp.route('/imports/<int:id>', methods = ['DELETE'])
def delete_import(id=None):
	info = import_info(id)

	if not info:
		return "Not found", 404

	rmtree(import_path(id))

	return "OK", 200

@bp.route('/imports', methods = ['GET'])
def list_imports():
	result = [import_info(import_id, include_running = True) for import_id in os.listdir(imports_path)]
	return jsonify(result)

@bp.route('/imports/<int:id>', methods = ['GET'])
def get_import(id=None):
	info = import_info(id, include_running = True)

	if not info:
		return "Not found", 404

	return jsonify(info)

@bp.route('/imports/<int:id>/log', methods = ['GET'])
def get_import_log(id=None):
	return send_file(os.path.join(import_path(id), 'import.log'), mimetype = 'text/plain')

def import_path(id):
	return os.path.join(imports_path, "%04d" % int(id))

def import_info_path(id):
	return os.path.join(import_path(id), 'status.json')

def save_import_info(import_id, info):
	import_id = int(import_id)
	with open(import_info_path(import_id), 'w') as file:
		json.dump(info, file)

def update_import_info(import_id, new_info):
	info = import_info(import_id)
	info.update(new_info)
	save_import_info(import_id, info)

def import_info(import_id, include_running = False):
	try:
		import_id = int(import_id)
		with open(import_info_path(import_id), 'r') as file:
			info = json.load(file)

		if info and include_running and import_id in running_imports:
			extra = running_imports[import_id]
			info['total_rows'] = extra['ns'].total_rows
			info['processed_rows'] = extra['ns'].processed_rows
			info['status'] = extra['status']
			info['started'] = extra['started']

		return info

	except:
		return None

# Note this must come after 'process_import' is defined, otherwise multiprocessing doesn't work
import_processing_pool = Pool()
