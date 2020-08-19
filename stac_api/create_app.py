"""fastapi app creation"""
from typing import Callable, Type

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from stac_api.clients.base import BaseTransactionsClient
from stac_api.clients.postgres.transactions import TransactionsClient
from stac_api.config import ApiSettings, inject_settings
from stac_api.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from stac_api.models import schemas
from stac_api.models.api import APIRequest, APIResponse, DeleteCollection, DeleteItem
from stac_api.resources import collection, conformance, item, mgmt
from stac_api.utils.dependencies import READER, WRITER, discover_base_url


# TODO: Only use one endpoint factory
def create_endpoint_from_model(
    func: Callable, request_model: Type[BaseModel], response_model: Type[APIResponse]
) -> Callable:
    """
    Create a FastAPI endpoint where request model is a pydantic model.  This works best for validating request bodies
    (POST/PUT etc.)
    """

    def _endpoint(
        request_data: request_model,  # type:ignore
        base_url: str = Depends(discover_base_url),  # type:ignore
    ):
        """endpoint"""
        resp = func(request_data)
        return response_model.create_api_response(resp, base_url)

    return _endpoint


def create_endpoint_with_depends(
    func: Callable, request_model: Type[APIRequest], response_model: Type[APIResponse]
) -> Callable:
    """
    Create a fastapi endpoint where request model is a dataclass.  This works best for validating query/patm params.
    """

    def _endpoint(
        request_data: request_model = Depends(),  # type:ignore
        base_url: str = Depends(discover_base_url),
    ):
        """endpoint"""
        resp = func(**request_data.kwargs())  # type:ignore
        return response_model.create_api_response(resp, base_url)

    return _endpoint


def create_transactions_router(client: BaseTransactionsClient) -> APIRouter:
    """Create API router for transactions extension"""
    router = APIRouter()
    router.add_api_route(
        name="Create Item",
        path="/collections/{collectionId}/items",
        response_model=schemas.Item,
        response_model_exclude_unset=True,
        response_model_exclude_none=True,
        methods=["POST"],
        endpoint=create_endpoint_from_model(
            client.create_item, schemas.Item, schemas.Item
        ),
    )
    router.add_api_route(
        name="Update Item",
        path="/collections/{collectionId}/items",
        response_model=schemas.Item,
        response_model_exclude_unset=True,
        response_model_exclude_none=True,
        methods=["PUT"],
        endpoint=create_endpoint_from_model(
            client.update_item, schemas.Item, schemas.Item
        ),
    )
    router.add_api_route(
        name="Delete Item",
        path="/collections/{collectionId}/items/{itemId}",
        response_model=schemas.Item,
        response_model_exclude_unset=True,
        response_model_exclude_none=True,
        methods=["DELETE"],
        endpoint=create_endpoint_with_depends(
            client.delete_item, DeleteItem, schemas.Item
        ),
    )
    router.add_api_route(
        name="Create Collection",
        path="/collections",
        response_model=schemas.Collection,
        response_model_exclude_unset=True,
        response_model_exclude_none=True,
        methods=["POST"],
        endpoint=create_endpoint_from_model(
            client.create_collection, schemas.Collection, schemas.Collection
        ),
    )
    router.add_api_route(
        name="Update Collection",
        path="/collections",
        response_model=schemas.Collection,
        response_model_exclude_unset=True,
        response_model_exclude_none=True,
        methods=["PUT"],
        endpoint=create_endpoint_from_model(
            client.update_collection, schemas.Collection, schemas.Collection
        ),
    )
    router.add_api_route(
        name="Delete Collection",
        path="/collections/{collectionId}",
        response_model=schemas.Collection,
        response_model_exclude_unset=True,
        response_model_exclude_none=True,
        methods=["DELETE"],
        endpoint=create_endpoint_with_depends(
            client.delete_collection, DeleteCollection, schemas.Collection
        ),
    )
    return router


def create_app(settings: ApiSettings, transactions=False) -> FastAPI:
    """Create a FastAPI app"""
    app = FastAPI()
    inject_settings(settings)

    app.debug = settings.debug
    app.include_router(mgmt.router)
    app.include_router(conformance.router)
    app.include_router(collection.router)
    app.include_router(item.router)
    add_exception_handlers(app, DEFAULT_STATUS_CODES)

    if transactions:
        transaction_client = TransactionsClient()
        app.include_router(create_transactions_router(transaction_client))

    @app.on_event("startup")
    async def on_startup():
        """Create database engines and sessions on startup"""
        app.state.ENGINE_READER = create_engine(settings.reader_connection_string)
        app.state.ENGINE_WRITER = create_engine(settings.writer_connection_string)
        app.state.DB_READER = sessionmaker(
            autocommit=False, autoflush=False, bind=app.state.ENGINE_READER
        )
        app.state.DB_WRITER = sessionmaker(
            autocommit=False, autoflush=False, bind=app.state.ENGINE_WRITER
        )

    @app.on_event("shutdown")
    async def on_shutdown():
        """Dispose of database engines and sessions on app shutdown"""
        app.state.ENGINE_READER.dispose()
        app.state.ENGINE_WRITER.dispose()

    @app.middleware("http")
    async def create_db_connection(request: Request, call_next):
        """Create a new database connection for each request"""
        reader = request.app.state.DB_READER()
        writer = request.app.state.DB_WRITER()
        READER.set(reader)
        WRITER.set(writer)
        resp = await call_next(request)
        reader.close()
        writer.close()
        return resp

    return app