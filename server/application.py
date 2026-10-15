"""Construct two HTTP surfaces with one device store and one durable worker."""
from server.app import create_app
from server.recipes.config import ProcessingConfig
from server.recipes.processing import Worker
from server.api.library import ApiLibrary
from server.api.device import DeviceStore
from server.api.http import application_origin, create_web_app
from server.api.service import Service
from server.calendar import CalendarConfig, CalendarWorker


def create_apps(directory, base_url, app_origin='', interval=20, device_id=None, *, config=None, calendar_config=None):
    origin = application_origin(app_origin)
    library = ApiLibrary(directory)
    calendar_config = calendar_config or CalendarConfig()
    store = DeviceStore(library,base_url,interval,device_id,calendar_config)
    service = Service(library,store,origin,config if config is not None else ProcessingConfig.from_env())
    lan = create_app(directory,base_url,store=store,service=service)
    web = create_web_app(service)
    worker = Worker(library,service.config)
    service.calendar_worker = CalendarWorker(store, calendar_config)
    return lan,web,service,worker
