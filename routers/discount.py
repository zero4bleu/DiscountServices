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


EXTERNAL_PRODUCTS_API_URL = "https://ims-productservices.onrender.com/is_products/products/details/" 
AUTH_SERVICE_ME_URL = "https://authservices-npr8.onrender.com/auth/users/me"
BLOCKCHAIN_SERVICE_URL = "https://blockchainservices.onrender.com/blockchain/log"


router = APIRouter() 
discounts_router = APIRouter(prefix="/discounts", tags=["Discounts"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="https://authservices-npr8.onrender.com/auth/token")


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

# BLOCKCHAIN LOGGING HELPER - Now runs in background
async def log_to_blockchain_async(
    token: str,
    action: str,
    entity_id: int,
    actor_username: str,
    change_description: str,
    data: dict
):
    """
    Log discount operations to blockchain (async background task)
    """
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "service_identifier": "DISCOUNTS_SERVICE",
        "action": action,
        "entity_type": "Discount",
        "entity_id": entity_id,
        "actor_username": actor_username,
        "change_description": change_description,
        "data": data
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                BLOCKCHAIN_SERVICE_URL,
                json=payload,
                headers=headers
            )
            response.raise_for_status()
    except Exception as e:
        print(f"⚠️  Blockchain logging failed: {e}")

# HELPER FUNCTION TO AUTO-EXPIRE DISCOUNTS
async def auto_expire_discounts(conn):
    """Automatically updates discount status to 'expired' if validTo date has passed"""
    try:
        async with conn.cursor() as cursor:
            today = date.today()
            sql_expire = """
                UPDATE discounts 
                SET status = 'expired', updated_at = GETDATE()
                WHERE valid_to < ? AND status != 'expired' AND isDeleted = 0
            """
            await cursor.execute(sql_expire, today)
            await conn.commit()
    except Exception as e:
        print(f"Error auto-expiring discounts: {e}")

# PYDANTIC MODELS
class DiscountBase(BaseModel):
    discountName: str = Field(..., max_length=255)
    applicationType: Literal['all_products', 'specific_categories', 'specific_products']
    selectedCategories: Optional[List[str]] = []
    selectedProducts: Optional[List[str]] = []
    discountType: Literal['percentage', 'fixed_amount']
    discountValue: Decimal = Field(..., gt=0)
    minSpend: Optional[Decimal] = Field(0, ge=0)
    validFrom: date
    validTo: date
    status: Literal['active', 'inactive', 'expired']

class DiscountCreate(DiscountBase): pass
class DiscountUpdate(DiscountBase): pass

class DiscountListOut(BaseModel):
    id: int
    name: str
    application: str
    discount: str
    minSpend: float
    validFrom: str
    validTo: str
    status: str
    type: str
    application_type: str
    applicable_products: List[str]
    applicable_categories: List[str]

class DiscountDetailOut(DiscountBase):
    id: int

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

# DISCOUNT ENDPOINTS

@discounts_router.post("/", response_model=DiscountDetailOut, status_code=status.HTTP_201_CREATED)
async def create_discount(
    discount_data: DiscountCreate, 
    background_tasks: BackgroundTasks,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, allowed_roles=["admin"])
    conn = await get_db_connection()
    try:
        conn.autocommit = False
        async with conn.cursor() as cursor:
            sql_insert = """
                INSERT INTO discounts (name, status, application_type, discount_type, discount_value, minimum_spend, valid_from, valid_to, isDeleted)
                OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0);
            """
            await cursor.execute(sql_insert, discount_data.discountName, discount_data.status, discount_data.applicationType,
                                 discount_data.discountType, discount_data.discountValue, discount_data.minSpend,
                                 discount_data.validFrom.isoformat(), discount_data.validTo.isoformat())
            new_id = (await cursor.fetchone())[0]

            if discount_data.applicationType == 'specific_products':
                for name in discount_data.selectedProducts: 
                    await cursor.execute("INSERT INTO discount_applicable_products (discount_id, product_name) VALUES (?, ?)", new_id, name)
            elif discount_data.applicationType == 'specific_categories':
                for name in discount_data.selectedCategories: 
                    await cursor.execute("INSERT INTO discount_applicable_categories (discount_id, category_name) VALUES (?, ?)", new_id, name)
            
            await conn.commit()
            
            # Removed blockchain logging 
            #blockchain_data = {
            #    "id": new_id,
            #    "name": discount_data.discountName,
            #    "status": discount_data.status,
            #    "application_type": discount_data.applicationType,
            #    "discount_type": discount_data.discountType,
            #    "discount_value": str(discount_data.discountValue),
            #    "minimum_spend": str(discount_data.minSpend),
            #    "valid_from": discount_data.validFrom.isoformat(),
            #    "valid_to": discount_data.validTo.isoformat(),
            #    "selected_products": discount_data.selectedProducts,
            #    "selected_categories": discount_data.selectedCategories
            #}
            #
            #background_tasks.add_task(
            #    log_to_blockchain_async,
            #    token, "CREATE", new_id, user_data.get("username", "unknown"),
            #    f"Created discount: {discount_data.discountName}", blockchain_data
            #)
            
            return DiscountDetailOut(id=new_id, **discount_data.model_dump())
    except Exception as e:
        await conn.rollback()
        if "UNIQUE" in str(e).upper(): 
            raise HTTPException(status_code=409, detail=f"A discount with the name '{discount_data.discountName}' already exists.")
        raise HTTPException(status_code=500, detail=f"Database error on create: {e}")
    finally:
        conn.autocommit = True
        if conn: await conn.close()

@discounts_router.get("/", response_model=List[DiscountListOut])
async def get_all_discounts(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, allowed_roles=["admin", "manager", "cashier"])
    conn = await get_db_connection()
    try:
        await auto_expire_discounts(conn)
        
        async with conn.cursor() as cursor:
            sql = """
                SELECT 
                    d.id, d.name, d.status, d.application_type, d.discount_type, 
                    d.discount_value, d.minimum_spend, d.valid_from, d.valid_to,
                    (
                        STUFF((SELECT DISTINCT ',' + dp.product_name
                               FROM discount_applicable_products dp
                               WHERE dp.discount_id = d.id
                               FOR XML PATH('')), 1, 1, '')
                    ) as products,
                    (
                        STUFF((SELECT DISTINCT ',' + dc.category_name
                               FROM discount_applicable_categories dc
                               WHERE dc.discount_id = d.id
                               FOR XML PATH('')), 1, 1, '')
                    ) as categories
                FROM discounts d
                WHERE d.isDeleted = 0
                ORDER BY d.id DESC
            """
            await cursor.execute(sql)
            rows = await cursor.fetchall()
            
            results = []
            for row in rows:
                prods = row.products.split(',') if row.products else []
                cats = row.categories.split(',') if row.categories else []
                
                app_str = "All Products"
                if row.application_type == 'specific_products': 
                    app_str = f"{len(prods)} Product(s)"
                elif row.application_type == 'specific_categories': 
                    app_str = f"{len(cats)} Category(s)"
                
                disc_str = f"₱{row.discount_value:.2f}"
                if row.discount_type == 'percentage': 
                    disc_str = f"{row.discount_value:.1f}%"
                
                results.append(DiscountListOut(
                    id=row.id, 
                    name=row.name, 
                    application=app_str, 
                    discount=disc_str, 
                    minSpend=float(row.minimum_spend), 
                    # ✅ FIX: Use .date() here as well for the list view
                    validFrom=row.valid_from.date().strftime('%Y-%m-%d'), 
                    validTo=row.valid_to.date().strftime('%Y-%m-%d'), 
                    status=row.status, 
                    type=row.discount_type,
                    application_type=row.application_type,
                    applicable_products=prods,
                    applicable_categories=cats
                ))
            return results
    except Exception as e:
        print(f"An unexpected error occurred in get_all_discounts: {e}")
        raise HTTPException(status_code=500, detail=f"Database error on get all: {e}")
    finally:
        if conn: await conn.close()
        
@discounts_router.get("/{discount_id}", response_model=DiscountDetailOut)
async def get_discount(discount_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, allowed_roles=["admin", "manager", "cashier"])
    conn = await get_db_connection()
    try:
        await auto_expire_discounts(conn)
        
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM discounts WHERE id=? AND isDeleted = 0", discount_id)
            d = await cursor.fetchone()
            if not d: raise HTTPException(status_code=404, detail="Discount not found")
            
            base_data = dict(zip([c[0] for c in cursor.description], d))
            await cursor.execute("SELECT product_name FROM discount_applicable_products WHERE discount_id=?", discount_id)
            products = [row.product_name for row in await cursor.fetchall()]
            await cursor.execute("SELECT category_name FROM discount_applicable_categories WHERE discount_id=?", discount_id)
            categories = [row.category_name for row in await cursor.fetchall()]

            # Convert datetime from database to date before Pydantic validation
            return DiscountDetailOut(
                id=base_data['id'], 
                discountName=base_data['name'], 
                applicationType=base_data['application_type'],
                selectedProducts=products, 
                selectedCategories=categories, 
                discountType=base_data['discount_type'],
                discountValue=base_data['discount_value'], 
                minSpend=base_data['minimum_spend'],
                validFrom=base_data['valid_from'].date(),  
                validTo=base_data['valid_to'].date(),      
                status=base_data['status']
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error on get one: {e}")
    finally:
        if conn: await conn.close()


@discounts_router.put("/{discount_id}", response_model=DiscountDetailOut)
async def update_discount(
    discount_id: int, 
    discount_data: DiscountUpdate, 
    background_tasks: BackgroundTasks,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, allowed_roles=["admin"])
    conn = await get_db_connection()
    try:
        conn.autocommit = False
        async with conn.cursor() as cursor:
            # Get old data for comparison
            await cursor.execute("SELECT * FROM discounts WHERE id=? AND isDeleted = 0", discount_id)
            old_discount = await cursor.fetchone()
            if not old_discount:
                raise HTTPException(status_code=404, detail="Discount not found")
            
            old_data = dict(zip([c[0] for c in cursor.description], old_discount))
            
            sql_update = """
                UPDATE discounts SET name=?, status=?, application_type=?, discount_type=?, discount_value=?, minimum_spend=?, valid_from=?, valid_to=?, updated_at=GETDATE()
                WHERE id=? AND isDeleted = 0
            """
            await cursor.execute(sql_update, discount_data.discountName, discount_data.status, discount_data.applicationType,
                                 discount_data.discountType, discount_data.discountValue, discount_data.minSpend,
                                 discount_data.validFrom.isoformat(), discount_data.validTo.isoformat(), discount_id)
            if cursor.rowcount == 0: raise HTTPException(status_code=404, detail="Discount not found")

            await cursor.execute("DELETE FROM discount_applicable_products WHERE discount_id=?", discount_id)
            await cursor.execute("DELETE FROM discount_applicable_categories WHERE discount_id=?", discount_id)

            if discount_data.applicationType == 'specific_products':
                for name in discount_data.selectedProducts: 
                    await cursor.execute("INSERT INTO discount_applicable_products (discount_id, product_name) VALUES (?, ?)", discount_id, name)
            elif discount_data.applicationType == 'specific_categories':
                for name in discount_data.selectedCategories: 
                    await cursor.execute("INSERT INTO discount_applicable_categories (discount_id, category_name) VALUES (?, ?)", discount_id, name)

            await conn.commit()
            
            # Build change description
            changes = []
            if old_data['name'] != discount_data.discountName:
                changes.append(f"name: '{old_data['name']}' → '{discount_data.discountName}'")
            if old_data['status'] != discount_data.status:
                changes.append(f"status: '{old_data['status']}' → '{discount_data.status}'")
            if old_data['discount_value'] != discount_data.discountValue:
                changes.append(f"value: {old_data['discount_value']} → {discount_data.discountValue}")
            
            change_desc = f"Updated discount: {', '.join(changes) if changes else 'modified fields'}"
            
            # Removed blockchain logging
            #blockchain_data = {
            #    "id": discount_id,
            #    "name": discount_data.discountName,
            #    "status": discount_data.status,
            #    "application_type": discount_data.applicationType,
            #    "discount_type": discount_data.discountType,
            #    "discount_value": str(discount_data.discountValue),
            #    "minimum_spend": str(discount_data.minSpend),
            #    "valid_from": discount_data.validFrom.isoformat(),
            #    "valid_to": discount_data.validTo.isoformat(),
            #    "selected_products": discount_data.selectedProducts,
            #    "selected_categories": discount_data.selectedCategories,
            #    "previous_values": {
            #        "name": old_data['name'],
            #        "status": old_data['status'],
            #        "discount_value": str(old_data['discount_value'])
            #    }
            #}
            #
            #background_tasks.add_task(
            #    log_to_blockchain_async,
            #    token, "UPDATE", discount_id, user_data.get("username", "unknown"),
            #    change_desc, blockchain_data
            #)
            
            return DiscountDetailOut(id=discount_id, **discount_data.model_dump())
    except Exception as e:
        await conn.rollback()
        if "UNIQUE" in str(e).upper(): 
            raise HTTPException(status_code=409, detail=f"A discount with the name '{discount_data.discountName}' already exists.")
        raise HTTPException(status_code=500, detail=f"Database error on update: {e}")
    finally:
        conn.autocommit = True
        if conn: await conn.close()

@discounts_router.delete("/{discount_id}", status_code=status.HTTP_200_OK)
async def delete_discount(
    discount_id: int, 
    background_tasks: BackgroundTasks,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, allowed_roles=["admin"])
    conn = await get_db_connection()
    try:
        conn.autocommit = False
        async with conn.cursor() as cursor:
            # Get discount data before deletion
            await cursor.execute("SELECT name FROM discounts WHERE id=? AND isDeleted = 0", discount_id)
            discount = await cursor.fetchone()
            if not discount:
                raise HTTPException(status_code=404, detail="Discount not found")
            
            discount_name = discount.name
            
            # Soft delete
            await cursor.execute("UPDATE discounts SET isDeleted = 1, updated_at = GETDATE() WHERE id = ? AND isDeleted = 0", discount_id)
            if cursor.rowcount == 0: 
                raise HTTPException(status_code=404, detail="Discount not found")
            
            await conn.commit()
            
            # Removed blockchain logging
            #blockchain_data = {
            #    "id": discount_id,
            #    "name": discount_name,
            #    "deleted": True,
            #    "deleted_at": datetime.now().isoformat()
            #}
            #
            #background_tasks.add_task(
            #    log_to_blockchain_async,
            #    token, "DELETE", discount_id, user_data.get("username", "unknown"),
            #    f"Deleted discount: {discount_name}", blockchain_data
            #)
            
            return {"message": "Discount deleted successfully."}
    except Exception as e:
        await conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error on delete: {e}")
    finally:
        conn.autocommit = True
        if conn: await conn.close()


@router.get("/available-products", response_model=List[dict], tags=["External Data"])
async def get_available_products_for_frontend(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, allowed_roles=["admin", "manager", "cashier"])
    valid_products, _ = await get_external_choices(token=token)
    return [{"ProductName": name} for name in sorted(list(valid_products))]

@router.get("/available-categories", response_model=List[dict], tags=["External Data"])
async def get_available_categories_for_frontend(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, allowed_roles=["admin", "manager", "cashier"])
    _, valid_categories = await get_external_choices(token=token)
    return [{"name": name} for name in sorted(list(valid_categories))]

router.include_router(discounts_router)
