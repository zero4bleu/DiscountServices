from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from decimal import Decimal
from datetime import date, datetime
import httpx 

# --- Database Connection Import ---
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db_connection

EXTERNAL_PRODUCTS_API_URL = "http://127.0.0.1:8001/is_products/products/details/" 
AUTH_SERVICE_ME_URL = "http://localhost:4000/auth/users/me"
BLOCKCHAIN_LOG_URL = os.getenv("BLOCKCHAIN_LOG_URL", "http://localhost:9005/blockchain/log")

router = APIRouter() 
promotions_router = APIRouter(prefix="/promotions", tags=["Promotions"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost:4000/auth/token")

# AUTHORIZATION HELPER
async def validate_token_and_roles(token: str, allowed_roles: List[str]):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(AUTH_SERVICE_ME_URL, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Authentication service error: {e.response.text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Authentication service is unavailable: {e}")

    user_data = response.json()
    user_role = user_data.get("userRole")

    if user_role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Access denied. Role '{user_role}' is not authorized.")
    
    return user_data

# HELPER FUNCTION TO SERIALIZE DATA FOR BLOCKCHAIN
def serialize_for_blockchain(data: dict) -> dict:
    """Convert data types to JSON-serializable format"""
    import json
    
    def convert_value(value):
        if isinstance(value, Decimal):
            return str(value)
        elif isinstance(value, (date, datetime)):
            return value.isoformat()
        elif isinstance(value, dict):
            return {k: convert_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [convert_value(item) for item in value]
        return value
    
    return convert_value(data)

# BLOCKCHAIN LOGGING HELPER - OPTIMIZED to run in the background
async def log_to_blockchain_background(
    token: str,
    action: str,
    entity_id: int,
    actor_username: str,
    change_description: str,
    data: dict
):
    """Log promotion operations to blockchain as a background task"""
    headers = {"Authorization": f"Bearer {token}"}
    
    # Serialize data to handle Decimal and date types
    serialized_data = serialize_for_blockchain(data)
    
    payload = {
        "service_identifier": "PROMOTIONS",
        "action": action,
        "entity_type": "Promotion",
        "entity_id": entity_id,
        "actor_username": actor_username,
        "change_description": change_description,
        "data": serialized_data
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(BLOCKCHAIN_LOG_URL, json=payload, headers=headers)
            response.raise_for_status()
    except Exception as e:
        print(f"⚠️  Blockchain logging failed: {e}")

# HELPER FUNCTION TO AUTO-EXPIRE PROMOTIONS
async def auto_expire_promotions(conn):
    """Automatically updates promotion status to 'expired' if validTo date has passed"""
    try:
        async with conn.cursor() as cursor:
            today = date.today()
            sql_expire = """
                UPDATE promotions 
                SET status = 'expired', updated_at = GETDATE()
                WHERE valid_to < ? AND status != 'expired' AND isDeleted = 0
            """
            await cursor.execute(sql_expire, today)
            await conn.commit()
    except Exception as e:
        print(f"Error auto-expiring promotions: {e}")

# PYDANTIC MODELS
class PromotionBase(BaseModel):
    promotionName: str = Field(..., max_length=255)
    description: Optional[str] = None
    applicationType: Literal['all_products', 'specific_categories', 'specific_products'] = 'specific_products'
    selectedProducts: List[str] = []
    selectedCategories: List[str] = []
    promotionType: Literal['percentage', 'fixed', 'bogo']
    promotionValue: Optional[Decimal] = Field(None, gt=0)
    buyQuantity: Optional[int] = Field(1, ge=1)
    getQuantity: Optional[int] = Field(1, ge=1)
    bogoDiscountType: Optional[Literal['percentage', 'fixed_amount']] = None
    bogoDiscountValue: Optional[Decimal] = Field(None, gt=0)
    bogoPromotionImage: Optional[str] = None  # ✅ ADDED
    minQuantity: Optional[int] = Field(None, ge=1)
    validFrom: date
    validTo: date
    status: Literal['active', 'inactive', 'expired']

class PromotionCreate(PromotionBase): pass
class PromotionUpdate(PromotionBase): pass

class PromotionDetailOut(PromotionBase):
    id: int

class BogoProductInfo(BaseModel):
    product_name: str

class PromotionListOut(BaseModel):
    id: int
    name: str
    type: str
    value: str
    products: str
    validFrom: str
    validTo: str
    status: str
    bogoProducts: Optional[List[BogoProductInfo]] = None
    buyQuantity: Optional[int] = None  # ✅ ADD THIS
    getQuantity: Optional[int] = None  # ✅ ADD THIS
    bogoDiscountType: Optional[str] = None  # ✅ ADD THIS
    bogoDiscountValue: Optional[Decimal] = None  # ✅ ADD THIS
    bogoPromotionImage: Optional[str] = None  # ✅ ADD THIS
    description: Optional[str] = None  # ✅ ADD THIS

# HELPER FUNCTION FOR EXTERNAL DATA 
async def get_external_choices(token: str):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(EXTERNAL_PRODUCTS_API_URL, headers=headers, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            valid_products = {item['ProductName'] for item in data if 'ProductName' in item and item['ProductName']}
            valid_categories = {item['ProductCategory'] for item in data if 'ProductCategory' in item and item['ProductCategory']}
            return valid_products, valid_categories
    except httpx.RequestError as e:
        detail = f"Network error communicating with Products service: {e}"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
    except httpx.HTTPStatusError as e:
        detail = f"Products service returned an error: Status {e.response.status_code} - Response: {e.response.text}"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

# PROMOTION ENDPOINTS

@promotions_router.post("/", response_model=PromotionDetailOut, status_code=status.HTTP_201_CREATED)
async def create_promotion(
    promotion_data: PromotionCreate, 
    background_tasks: BackgroundTasks,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, allowed_roles=["admin"])
    
    # Validation logic
    if promotion_data.promotionType == 'bogo':
        promotion_data.applicationType = 'specific_products'
        if not promotion_data.selectedProducts:
            raise HTTPException(status_code=400, detail="At least one product must be selected for BOGO")
        if len(promotion_data.selectedProducts) > 2:
            raise HTTPException(status_code=400, detail="BOGO promotions can have a maximum of 2 products")
        if not promotion_data.bogoDiscountType or promotion_data.bogoDiscountValue is None:
            raise HTTPException(status_code=400, detail="BOGO discount type and value are required")
    else:
        if promotion_data.promotionValue is None:
            raise HTTPException(status_code=400, detail="Promotion value is required")
        if promotion_data.applicationType == 'specific_categories' and not promotion_data.selectedCategories:
            raise HTTPException(status_code=400, detail="At least one category must be selected")
        elif promotion_data.applicationType == 'specific_products' and not promotion_data.selectedProducts:
            raise HTTPException(status_code=400, detail="At least one product must be selected")
    
    conn = await get_db_connection()
    try:
        conn.autocommit = False
        async with conn.cursor() as cursor:
            sql_insert = """
                INSERT INTO promotions (name, description, application_type, promotion_type, promotion_value, 
                 buy_quantity, get_quantity, bogo_discount_type, bogo_discount_value, bogo_promotion_image, 
                 min_quantity, valid_from, valid_to, status, isDeleted)
                OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0);
            """
            await cursor.execute(sql_insert, 
                promotion_data.promotionName, 
                promotion_data.description, 
                promotion_data.applicationType,
                promotion_data.promotionType, 
                promotion_data.promotionValue, 
                promotion_data.buyQuantity, 
                promotion_data.getQuantity,
                promotion_data.bogoDiscountType, 
                promotion_data.bogoDiscountValue,
                promotion_data.bogoPromotionImage,  # ✅ ADDED
                promotion_data.minQuantity,
                promotion_data.validFrom.isoformat(), 
                promotion_data.validTo.isoformat(), 
                promotion_data.status
            )
            new_id = (await cursor.fetchone())[0]

            # Insert products
            for product_name in promotion_data.selectedProducts:
                await cursor.execute(
                    "INSERT INTO promotion_applicable_products (promotion_id, product_name) VALUES (?, ?)", 
                    new_id, product_name
                )
            
            for category_name in promotion_data.selectedCategories:
                await cursor.execute("INSERT INTO promotion_applicable_categories (promotion_id, category_name) VALUES (?, ?)", new_id, category_name)
            
            await conn.commit()
            
            return PromotionDetailOut(id=new_id, **promotion_data.model_dump())
    except Exception as e:
        await conn.rollback()
        if "UNIQUE" in str(e).upper():
            raise HTTPException(status_code=409, detail=f"A promotion with the name '{promotion_data.promotionName}' already exists.")
        raise HTTPException(status_code=500, detail=f"Database error on create: {e}")
    finally:
        conn.autocommit = True
        if conn: await conn.close()

@promotions_router.get("/", response_model=List[PromotionListOut])
async def get_all_promotions(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, allowed_roles=["admin", "manager", "cashier"])
    conn = await get_db_connection()
    try:
        await auto_expire_promotions(conn)
        
        async with conn.cursor() as cursor:
            sql = """
                SELECT 
                    p.id, p.name, p.application_type, p.promotion_type, p.promotion_value, p.buy_quantity, 
                    p.get_quantity, p.bogo_discount_type, p.bogo_discount_value, p.valid_from, 
                    p.valid_to, p.status,
                    (
                        STUFF((SELECT DISTINCT ', ' + pp.product_name
                               FROM promotion_applicable_products pp
                               WHERE pp.promotion_id = p.id
                               FOR XML PATH('')), 1, 2, '')
                    ) as products,
                    (
                        STUFF((SELECT DISTINCT ', ' + pc.category_name
                               FROM promotion_applicable_categories pc
                               WHERE pc.promotion_id = p.id
                               FOR XML PATH('')), 1, 2, '')
                    ) as categories
                FROM promotions p
                WHERE p.isDeleted = 0
                ORDER BY p.id DESC
            """
            await cursor.execute(sql)
            promotions = await cursor.fetchall()
            
            results = []
            for p in promotions:
                type_str = p.promotion_type.upper()
                if p.promotion_type == 'bogo':
                    type_str = f"BOGO ({p.buy_quantity}+{p.get_quantity})"
                
                value_str = ""
                if p.promotion_type == 'percentage':
                    value_str = f"{p.promotion_value:.1f}%"
                elif p.promotion_type == 'fixed':
                    value_str = f"₱{p.promotion_value:.2f}"
                elif p.promotion_type == 'bogo':
                    if p.bogo_discount_type == 'percentage':
                        value_str = f"{p.bogo_discount_value:.1f}% off"
                    else:
                        value_str = f"₱{p.bogo_discount_value:.2f} off"
                
                products_str = ""
                bogo_products_list = None
                
                if p.application_type == 'all_products':
                    products_str = "All Products"
                elif p.application_type == 'specific_categories':
                    products_str = p.categories if p.categories else "N/A"
                else:
                    products_str = p.products if p.products else "N/A"
                    
                    # Fetch BOGO product info if it's a BOGO promotion
                    if p.promotion_type == 'bogo':
                        await cursor.execute(
                            "SELECT product_name FROM promotion_applicable_products WHERE promotion_id=?", 
                            p.id
                        )
                        bogo_rows = await cursor.fetchall()
                        bogo_products_list = [
                            BogoProductInfo(product_name=row.product_name) for row in bogo_rows
                        ]
                
                results.append(PromotionListOut(
                    id=p.id, name=p.name, type=type_str, value=value_str,
                    products=products_str,
                    validFrom=p.valid_from.strftime('%Y-%m-%d'),
                    validTo=p.valid_to.strftime('%Y-%m-%d'),
                    status=p.status,
                    bogoProducts=bogo_products_list
                ))
            return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error on get all: {e}")
    finally:
        if conn: await conn.close()

@promotions_router.get("/bogo", response_model=List[PromotionListOut])
async def get_bogo_promotions(token: str = Depends(oauth2_scheme)):
    """Get all active BOGO promotions with their images and details"""
    await validate_token_and_roles(token, allowed_roles=["admin", "manager", "cashier"])
    conn = await get_db_connection()
    try:
        await auto_expire_promotions(conn)
        
        async with conn.cursor() as cursor:
            sql = """
                SELECT 
                    p.id, p.name, p.description, p.application_type, p.promotion_type, 
                    p.promotion_value, p.buy_quantity, p.get_quantity, p.bogo_discount_type, 
                    p.bogo_discount_value, p.bogo_promotion_image, p.valid_from, 
                    p.valid_to, p.status,
                    (
                        STUFF((SELECT DISTINCT ', ' + pp.product_name
                               FROM promotion_applicable_products pp
                               WHERE pp.promotion_id = p.id
                               FOR XML PATH('')), 1, 2, '')
                    ) as products
                FROM promotions p
                WHERE p.isDeleted = 0 
                  AND p.promotion_type = 'bogo'
                  AND p.status = 'active'
                ORDER BY p.id DESC
            """
            await cursor.execute(sql)
            promotions = await cursor.fetchall()
            
            results = []
            for p in promotions:
                type_str = f"BOGO ({p.buy_quantity}+{p.get_quantity})"
                
                value_str = ""
                if p.bogo_discount_type == 'percentage':
                    value_str = f"{p.bogo_discount_value:.1f}% off"
                else:
                    value_str = f"₱{p.bogo_discount_value:.2f} off"
                
                products_str = p.products if p.products else "N/A"
                
                # Fetch BOGO product info
                await cursor.execute(
                    "SELECT product_name FROM promotion_applicable_products WHERE promotion_id=?", 
                    p.id
                )
                bogo_rows = await cursor.fetchall()
                bogo_products_list = [
                    BogoProductInfo(product_name=row.product_name) for row in bogo_rows
                ]
                
                results.append(PromotionListOut(
                    id=p.id, 
                    name=p.name, 
                    type=type_str, 
                    value=value_str,
                    products=products_str,
                    validFrom=p.valid_from.strftime('%Y-%m-%d'),
                    validTo=p.valid_to.strftime('%Y-%m-%d'),
                    status=p.status,
                    bogoProducts=bogo_products_list,
                    buyQuantity=p.buy_quantity,  # ✅ ADD THIS
                    getQuantity=p.get_quantity,  # ✅ ADD THIS
                    bogoDiscountType=p.bogo_discount_type,  # ✅ ADD THIS
                    bogoDiscountValue=p.bogo_discount_value,  # ✅ ADD THIS
                    bogoPromotionImage=p.bogo_promotion_image,  # ✅ ADD THIS
                    description=p.description  # ✅ ADD THIS
                ))
            return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error on get BOGO promotions: {e}")
    finally:
        if conn: await conn.close()

@promotions_router.get("/active", response_model=List[PromotionDetailOut])
async def get_active_promotions_public(token: str = Depends(oauth2_scheme)):
    """
    PUBLIC endpoint for OOS and other services.
    Requires user authentication.
    """
    await validate_token_and_roles(token, allowed_roles=["user"])
    conn = await get_db_connection()
    try:
        await auto_expire_promotions(conn)

        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT *
                FROM promotions
                WHERE status = 'active'
                  AND isDeleted = 0
                  AND valid_from <= CAST(GETDATE() AS DATE)
                  AND valid_to >= CAST(GETDATE() AS DATE)
            """)

            rows = await cursor.fetchall()
            results = []

            for row in rows:
                # Fetch related products
                await cursor.execute(
                    "SELECT product_name FROM promotion_applicable_products WHERE promotion_id=?",
                    row.id
                )
                products = [r.product_name for r in await cursor.fetchall()]

                # Fetch related categories
                await cursor.execute(
                    "SELECT category_name FROM promotion_applicable_categories WHERE promotion_id=?",
                    row.id
                )
                categories = [r.category_name for r in await cursor.fetchall()]

                results.append(PromotionDetailOut(
                    id=row.id,
                    promotionName=row.name,
                    description=row.description,
                    applicationType=row.application_type,
                    selectedProducts=products,
                    selectedCategories=categories,
                    promotionType=row.promotion_type,
                    promotionValue=row.promotion_value,
                    buyQuantity=row.buy_quantity,
                    getQuantity=row.get_quantity,
                    bogoDiscountType=row.bogo_discount_type,
                    bogoDiscountValue=row.bogo_discount_value,
                    bogoPromotionImage=row.bogo_promotion_image,
                    minQuantity=row.min_quantity,
                    validFrom=row.valid_from.date() if isinstance(row.valid_from, datetime) else row.valid_from,
                    validTo=row.valid_to.date() if isinstance(row.valid_to, datetime) else row.valid_to,
                    status=row.status
                ))

            return results
    finally:
        if conn:
            await conn.close()
        
@promotions_router.get("/{promotion_id}", response_model=PromotionDetailOut)
async def get_promotion(promotion_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, allowed_roles=["admin", "manager", "cashier"])
    conn = await get_db_connection()
    try:
        await auto_expire_promotions(conn)
        
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM promotions WHERE id=? AND isDeleted = 0", promotion_id)
            p = await cursor.fetchone()
            if not p: raise HTTPException(status_code=404, detail="Promotion not found")
            
            base_data = dict(zip([c[0] for c in cursor.description], p))
            
            await cursor.execute("SELECT product_name FROM promotion_applicable_products WHERE promotion_id=?", promotion_id)
            products = [row.product_name for row in await cursor.fetchall()]
            await cursor.execute("SELECT category_name FROM promotion_applicable_categories WHERE promotion_id=?", promotion_id)
            categories = [row.category_name for row in await cursor.fetchall()]

            return PromotionDetailOut(
                id=base_data['id'], 
                promotionName=base_data['name'], 
                description=base_data['description'],
                applicationType=base_data['application_type'], 
                selectedProducts=products,
                selectedCategories=categories, 
                promotionType=base_data['promotion_type'],
                promotionValue=base_data['promotion_value'], 
                buyQuantity=base_data['buy_quantity'],
                getQuantity=base_data['get_quantity'], 
                bogoDiscountType=base_data['bogo_discount_type'],
                bogoDiscountValue=base_data['bogo_discount_value'],
                bogoPromotionImage=base_data['bogo_promotion_image'],  # ✅ ADDED
                minQuantity=base_data['min_quantity'],
                validFrom=base_data['valid_from'].date(),  
                validTo=base_data['valid_to'].date(),      
                status=base_data['status']
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error on get one: {e}")
    finally:
        if conn: await conn.close()

@promotions_router.put("/{promotion_id}", response_model=PromotionDetailOut)
async def update_promotion(
    promotion_id: int, 
    promotion_data: PromotionUpdate, 
    background_tasks: BackgroundTasks,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, allowed_roles=["admin"])
    
    # Validation logic
    if promotion_data.promotionType == 'bogo':
        promotion_data.applicationType = 'specific_products'
        if not promotion_data.selectedProducts:
            raise HTTPException(status_code=400, detail="At least one product must be selected for BOGO")
        if len(promotion_data.selectedProducts) > 2:
            raise HTTPException(status_code=400, detail="BOGO promotions can have a maximum of 2 products")
        if not promotion_data.bogoDiscountType or promotion_data.bogoDiscountValue is None:
            raise HTTPException(status_code=400, detail="BOGO discount type and value are required")
    else:
        if promotion_data.promotionValue is None:
            raise HTTPException(status_code=400, detail="Promotion value is required")
        if promotion_data.applicationType == 'specific_categories' and not promotion_data.selectedCategories:
            raise HTTPException(status_code=400, detail="At least one category must be selected")
        elif promotion_data.applicationType == 'specific_products' and not promotion_data.selectedProducts:
            raise HTTPException(status_code=400, detail="At least one product must be selected")
    
    conn = await get_db_connection()
    try:
        conn.autocommit = False
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM promotions WHERE id=? AND isDeleted = 0", promotion_id)
            old_row = await cursor.fetchone()
            if not old_row: raise HTTPException(status_code=404, detail="Promotion not found")
            old_data = dict(zip([c[0] for c in cursor.description], old_row))
            
            sql_update = """
                UPDATE promotions SET 
                    name=?, description=?, application_type=?, promotion_type=?, promotion_value=?, 
                    buy_quantity=?, get_quantity=?, bogo_discount_type=?, bogo_discount_value=?, 
                    bogo_promotion_image=?, min_quantity=?, valid_from=?, valid_to=?, status=?, 
                    updated_at=GETDATE()
                WHERE id=? AND isDeleted = 0
            """
            await cursor.execute(sql_update, 
                promotion_data.promotionName, 
                promotion_data.description, 
                promotion_data.applicationType,
                promotion_data.promotionType, 
                promotion_data.promotionValue, 
                promotion_data.buyQuantity, 
                promotion_data.getQuantity,
                promotion_data.bogoDiscountType, 
                promotion_data.bogoDiscountValue,
                promotion_data.bogoPromotionImage,  # ✅ ADDED
                promotion_data.minQuantity,
                promotion_data.validFrom.isoformat(), 
                promotion_data.validTo.isoformat(), 
                promotion_data.status, 
                promotion_id
            )
            if cursor.rowcount == 0: raise HTTPException(status_code=404, detail="Promotion not found")

            # Delete and re-insert products
            await cursor.execute("DELETE FROM promotion_applicable_products WHERE promotion_id=?", promotion_id)
            for product_name in promotion_data.selectedProducts:
                await cursor.execute(
                    "INSERT INTO promotion_applicable_products (promotion_id, product_name) VALUES (?, ?)", 
                    promotion_id, product_name
                )
            
            await cursor.execute("DELETE FROM promotion_applicable_categories WHERE promotion_id=?", promotion_id)
            for category_name in promotion_data.selectedCategories:
                await cursor.execute("INSERT INTO promotion_applicable_categories (promotion_id, category_name) VALUES (?, ?)", promotion_id, category_name)

            await conn.commit()
            
            return PromotionDetailOut(id=promotion_id, **promotion_data.model_dump())
    except Exception as e:
        await conn.rollback()
        if "UNIQUE" in str(e).upper():
            raise HTTPException(status_code=409, detail=f"A promotion with the name '{promotion_data.promotionName}' already exists.")
        raise HTTPException(status_code=500, detail=f"Database error on update: {e}")
    finally:
        conn.autocommit = True
        if conn: await conn.close()

@promotions_router.delete("/{promotion_id}", status_code=status.HTTP_200_OK)
async def delete_promotion(
    promotion_id: int, 
    background_tasks: BackgroundTasks,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, allowed_roles=["admin"])
    conn = await get_db_connection()
    try:
        conn.autocommit = False
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM promotions WHERE id=? AND isDeleted = 0", promotion_id)
            row = await cursor.fetchone()
            if not row: raise HTTPException(status_code=404, detail="Promotion not found")
            promotion_data = dict(zip([c[0] for c in cursor.description], row))
            
            await cursor.execute("UPDATE promotions SET isDeleted = 1, updated_at = GETDATE() WHERE id = ? AND isDeleted = 0", promotion_id)
            if cursor.rowcount == 0: raise HTTPException(status_code=404, detail="Promotion not found")
            
            await conn.commit()
            
            return {"message": "Promotion deleted successfully."}
    except Exception as e:
        await conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error on delete: {e}")
    finally:
        conn.autocommit = True
        if conn: await conn.close()



router.include_router(promotions_router)