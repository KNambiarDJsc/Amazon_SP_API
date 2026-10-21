"""Worker entry point:  dramatiq app.workers.main --queues brand_checks"""

from app.workers.runtime import get_runtime

runtime = get_runtime()
process_brand = runtime.actor
