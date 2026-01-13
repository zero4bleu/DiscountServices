from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

# --- Import Routers ---
# Import the router from the discount file
from routers.discount import router as discount_router
# Import the router from the promotion file
from routers.promotions import router as promotion_router

app = FastAPI(
    title="Discount and Promotion Service API",
    description="API for managing all discount and promotion operations.",
    version="1.0.0"
)

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*",  # Self
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Include Routers ---
# This includes all endpoints from your discount.py file (e.g., /discounts/, /available-products)
app.include_router(discount_router, prefix="/api")
# This includes all endpoints from your promotion.py file (e.g., /promotions/)
app.include_router(promotion_router, prefix="/api")


# --- Static Files (Optional) ---
UPLOAD_DIR_NAME = "uploads"
os.makedirs(UPLOAD_DIR_NAME, exist_ok=True)
app.mount(f"/{UPLOAD_DIR_NAME}", StaticFiles(directory=UPLOAD_DIR_NAME), name=UPLOAD_DIR_NAME)


# --- Root Endpoint ---
@app.get("/", tags=["Root"])
async def read_root():
    return {"message": "Welcome to the Discount & Promotion Service API. Visit /docs for documentation."}


# --- Uvicorn Runner ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", port=9002, host="0.0.0.0", reload=True)
