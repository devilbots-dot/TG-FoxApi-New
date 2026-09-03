"""Analytics routes."""

from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.core.mongo import mongodb

router = APIRouter(tags=["Admin-Analytics"], include_in_schema=False)


@router.get("/admin/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/analytics.html", {"page": "analytics"})


@router.get("/admin/api/analytics/overview")
async def analytics_overview(_session=Depends(require_session), days: int = Query(30, ge=1, le=365)):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    orders = mongodb.orders
    deposits = mongodb.deposits
    users = mongodb.users
    sessions = mongodb.session_accounts

    # Revenue per day
    rev_pipeline = [
        {"$match": {"status": "completed", "completed_at": {"$gte": since}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$completed_at"}},
            "revenue": {"$sum": "$amount"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    rev_data = await orders.aggregate(rev_pipeline).to_list(length=days + 5)

    # New users per day
    user_pipeline = [
        {"$match": {"joined_at": {"$gte": since}, "user_id": {"$gt": 0}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$joined_at"}},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    user_data = await users.aggregate(user_pipeline).to_list(length=days + 5)

    # Sessions uploaded per day
    session_pipeline = [
        {"$match": {"uploaded_at": {"$gte": since}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$uploaded_at"}},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    session_data = await sessions.aggregate(session_pipeline).to_list(length=days + 5)

    # Top countries by orders sold
    country_pipeline = [
        {"$match": {"sold": True}},
        {"$group": {"_id": "$country_code", "count": {"$sum": 1}, "name": {"$first": "$country_name"}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    country_data = await sessions.aggregate(country_pipeline).to_list(length=10)

    # Deposit methods breakdown
    dep_method_pipeline = [
        {"$match": {"status": "completed"}},
        {"$group": {"_id": "$method", "count": {"$sum": 1}, "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}},
    ]
    dep_methods = await deposits.aggregate(dep_method_pipeline).to_list(length=20)

    # Spam status breakdown
    spam_pipeline = [
        {"$group": {"_id": "$spam_status", "count": {"$sum": 1}}},
    ]
    spam_data = await sessions.aggregate(spam_pipeline).to_list(length=10)

    # Rank distribution
    rank_pipeline = [
        {"$group": {"_id": "$rank", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    rank_data = await users.aggregate(rank_pipeline).to_list(length=10)

    return JSONResponse({
        "revenue_by_day": [{"date": d["_id"], "revenue": round(d["revenue"], 2), "orders": d["count"]} for d in rev_data],
        "users_by_day": [{"date": d["_id"], "count": d["count"]} for d in user_data],
        "sessions_by_day": [{"date": d["_id"], "count": d["count"]} for d in session_data],
        "top_countries": [{"code": d["_id"], "name": d.get("name", d["_id"]), "sold": d["count"]} for d in country_data],
        "deposit_methods": [{"method": d["_id"], "count": d["count"], "total": round(d["total"], 2)} for d in dep_methods],
        "spam_breakdown": [{"status": d["_id"] or "unknown", "count": d["count"]} for d in spam_data],
        "rank_distribution": [{"rank": d["_id"] or "VIP1", "count": d["count"]} for d in rank_data],
    })
