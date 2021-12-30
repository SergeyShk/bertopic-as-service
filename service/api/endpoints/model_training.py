from typing import List

import numpy as np
from aiobotocore.session import ClientCreatorContext
from fastapi import Depends, Query
from fastapi.exceptions import HTTPException
from fastapi.routing import APIRouter
from pydantic.types import UUID4
from sqlalchemy import func
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from service.api import deps
from service.api.utils import get_sample_dataset, load_model, save_model, save_topics
from service.core.config import settings
from service.models import models
from service.schemas.base import (
    DocsWithPredictions,
    FitResult,
    Input,
    ModelPrediction,
    NotEmptyInput,
)
from service.schemas.bertopic_wrapper import BERTopicWrapper

router = APIRouter(tags=["model_training"])


@router.post("/fit", summary="Run topic modeling", response_model=FitResult)
async def fit(
    data: Input,
    s3: ClientCreatorContext = Depends(deps.get_s3),
    session: AsyncSession = Depends(deps.get_db_async),
) -> FitResult:
    params = dict(data)
    texts = params.pop("texts")
    topic_model = BERTopicWrapper(**params).model
    if texts:
        topics, probs = topic_model.fit_transform(texts)
    else:
        docs = get_sample_dataset()
        topics, probs = topic_model.fit_transform(docs)

    model_id = await save_model(s3, topic_model)
    model = models.TopicModel(model_id=model_id)
    session.add(model)
    await save_topics(topic_model, session, model)
    await session.commit()
    return FitResult(
        model_id=model_id, predictions=ModelPrediction(topics=topics, probabilities=probs.tolist())
    )


@router.post("/predict", summary="Predict with existing model", response_model=ModelPrediction)
async def predict(
    data: NotEmptyInput,
    model_id: UUID4 = Query(...),
    version: int = 1,
    calculate_probabilities: bool = Query(default=False),
    s3: ClientCreatorContext = Depends(deps.get_s3),
) -> ModelPrediction:
    topic_model = await load_model(s3, model_id, version)
    topic_model.calculate_probabilities = calculate_probabilities
    topics, probabilities = topic_model.transform(data.texts)
    if probabilities is not None:
        probabilities = probabilities.tolist()
    return ModelPrediction(topics=topics, probabilities=probabilities)


@router.post(
    "/reduce_topics",
    summary="Reduce number of topics in existing model",
    response_model=FitResult,
)
async def reduce_topics(
    data: DocsWithPredictions,
    model_id: UUID4 = Query(...),
    version: int = Query(default=1),
    num_topics: int = Query(...),
    s3: ClientCreatorContext = Depends(deps.get_s3),
    session: AsyncSession = Depends(deps.get_db_async),
) -> FitResult:
    topic_model = await load_model(s3, model_id, version)
    if len(topic_model.get_topics()) < num_topics:
        raise HTTPException(
            status_code=400, detail=f"num_topics must be less than {len(topic_model.get_topics())}"
        )

    if len(data.texts) == 0:
        data.texts = get_sample_dataset()
    topics, probs = topic_model.reduce_topics(
        docs=data.texts,
        topics=data.topics,
        probabilities=np.array(data.probabilities),
        nr_topics=num_topics,
    )
    current_max_version = (
        await session.execute(
            select(models.TopicModel)
            .filter(models.TopicModel.model_id == model_id)
            .with_only_columns(func.max(models.TopicModel.version))
        )
    ).scalar() or 0
    model_id = await save_model(s3, topic_model, model_id, current_max_version + 1)
    model = models.TopicModel(model_id=model_id, version=current_max_version + 1)
    session.add(model)
    await save_topics(topic_model, session, model)
    await session.commit()

    return FitResult(
        model_id=model_id,
        version=current_max_version + 1,
        predictions=ModelPrediction(topics=topics, probabilities=probs.tolist()),
    )


@router.get(
    "/models", summary="Get all existing model ids", response_model=List[models.TopicModelBase]
)
async def list_models(
    session: AsyncSession = Depends(deps.get_db_async),
) -> List[models.TopicModel]:
    return (await session.execute(select(models.TopicModel))).scalars().all()


@router.get("/topics", summary="Get topics", response_model=List[models.TopicWithWords])
async def get_topics(
    model_id: UUID4 = Query(...),
    version: int = 1,
    session: AsyncSession = Depends(deps.get_db_async),
) -> List[models.TopicWithWords]:
    result = await session.execute(
        select(models.Topic)
        .join(models.TopicModel)
        .filter(models.TopicModel.model_id == model_id, models.TopicModel.version == version)
        .order_by(-models.Topic.count)
        .options(selectinload(models.Topic.top_words))
    )
    return result.scalars().all()


@router.get("/topics_info", summary="Get topics info", response_model=List[models.TopicBase])
async def get_topics_info(
    model_id: UUID4 = Query(...),
    version: int = 1,
    session: AsyncSession = Depends(deps.get_db_async),
) -> List[models.Topic]:
    return (
        (
            await session.execute(
                select(models.Topic)
                .join(models.TopicModel)
                .filter(
                    models.TopicModel.model_id == model_id, models.TopicModel.version == version
                )
                .order_by(-models.Topic.count)
            )
        )
        .scalars()
        .all()
    )


@router.get("/remove_model", summary="Remove topic model")
async def remove_model(
    model_id: UUID4 = Query(...),
    version: int = 1,
    s3: ClientCreatorContext = Depends(deps.get_s3),
    session: AsyncSession = Depends(deps.get_db_async),
) -> str:
    try:
        statement = select(models.TopicModel).filter(
            models.TopicModel.model_id == model_id, models.TopicModel.version == version
        )
        model = (await session.execute(statement)).scalars().first()
        await session.delete(model)
        await session.commit()

        await s3.delete_object(Bucket=settings.MINIO_BUCKET_NAME, Key=str(model_id))
    except (NoResultFound, s3.exceptions.NoSuchKey):
        raise HTTPException(status_code=404, detail="Model not found")
    return "ok"
