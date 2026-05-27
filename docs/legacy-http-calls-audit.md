# Legacy HTTP Calls Audit

This document tracks legacy `requests`/`httpx` calls that should be refactored to use the x-ui-client library.

## Summary

The codebase has been largely refactored to use the x-ui-client library, but some legacy HTTP calls remain for specific use cases.

## Functions Using Legacy HTTP Calls

### 1. `async_toggle_client_on_node()` - Line 762
**Status**: ⚠️ Should be refactored
**Location**: `/home/stein/python/3x-ui/central/admin/main.py:762`
**Usage**: Direct httpx calls to toggle client enable/disable
**Recommendation**: Add `toggle_client()` method to x-ui-client library

**Current Code**:
```python
async with httpx.AsyncClient(verify=False, timeout=30.0) as http_client:
    # Manual login, get inbounds, find client, update
```

**Proposed Refactor**:
```python
xui = await async_get_xui_client(node)
success = await async_xui_call(xui, 'toggle_client', client_email, enabled)
```

---

### 2. `async_backup_node()` - Line 1068
**Status**: ✅ OK (Special case)
**Location**: `/home/stein/python/3x-ui/central/admin/main.py:1068`
**Usage**: Downloads database file from node
**Recommendation**: Keep as-is (file download not part of x-ui API)

**Justification**: This function downloads the SQLite database file directly, which is not part of the 3x-ui panel API. It's a legitimate use of httpx for file transfer.

---

### 3. `delete_node()` - Line 1580
**Status**: ✅ OK (Quick test)
**Location**: `/home/stein/python/3x-ui/central/admin/main.py:1580`
**Usage**: Quick connectivity test before deletion
**Recommendation**: Keep as-is (lightweight check)

**Code**:
```python
async with httpx.AsyncClient(verify=False, timeout=2.0) as client:
    response = await client.get(f"{node.url}/panel", follow_redirects=True)
```

**Justification**: Simple connectivity test with short timeout. Adding to x-ui-client would be overkill.

---

### 4. `get_client_limit()` - Line 2149
**Status**: ⚠️ Should be refactored
**Location**: `/home/stein/python/3x-ui/central/admin/main.py:2149`
**Usage**: Gets IP limit for a client across nodes
**Recommendation**: Add `get_client_limit()` method to x-ui-client library

**Current Code**:
```python
session = requests.Session()
session.verify = False
response = session.post(...)
```

**Proposed Refactor**:
```python
xui = await async_get_xui_client(node)
limit = await async_xui_call(xui, 'get_client_ip_limit', client_email)
```

---

### 5. `update_client_limit()` - Line 2293
**Status**: ⚠️ Should be refactored
**Location**: `/home/stein/python/3x-ui/central/admin/main.py:2293`
**Usage**: Updates IP limit for a client
**Recommendation**: Add `update_client_limit()` method to x-ui-client library

**Current Code**:
```python
session = requests.Session()
session.verify = False
# Login, get inbounds, update client limit
```

**Proposed Refactor**:
```python
xui = await async_get_xui_client(node)
success = await async_xui_call(xui, 'update_client_ip_limit', client_email, limit_ip)
```

---

### 6. `update_user()` - Line 3544
**Status**: ⚠️ Should be refactored
**Location**: `/home/stein/python/3x-ui/central/admin/main.py:3544`
**Usage**: Updates client settings (payment status, limit)
**Recommendation**: Same as #5, consolidate with update_client_limit()

---

### 7. `check_renewals()` - Line 3801
**Status**: ⚠️ Should be refactored
**Location**: `/home/stein/python/3x-ui/central/admin/main.py:3801`
**Usage**: Background task to disable expired users
**Recommendation**: Same as #1, use toggle_client() method

---

## Refactoring Priority

### High Priority
1. **Add `toggle_client()` to x-ui-client** - Used in multiple places
2. **Add `update_client_ip_limit()` to x-ui-client** - IP limit management

### Medium Priority
3. **Add `get_client_ip_limit()` to x-ui-client** - For reading current limits

### Low Priority
- Keep `async_backup_node()` as-is (file download)
- Keep `delete_node()` connectivity test as-is (quick check)

## Migration Path

1. Add methods to `/home/stein/python/3x-ui/xui-cli-tool/x_ui_client/client.py`
2. Test methods with both v2.8.7 and v3.1.0
3. Update admin/main.py to use new methods
4. Remove legacy httpx/requests code

## Subscription Domain Usage

✅ **All subscription URL generation now uses `get_subscription_base_url(db)`**

No legacy `os.getenv("SUBSCRIPTION_URL")` calls remain outside the helper function.

---

## Summary Statistics

- **Total legacy HTTP functions**: 7
- **Should refactor**: 4 (57%)
- **OK to keep**: 2 (29%)
- **Subscription URLs**: ✅ Fully migrated

## Notes

- The x-ui-client library is working well and handles CSRF tokens correctly
- Legacy code predates the x-ui-client library implementation
- Most high-traffic operations (key creation, deletion, sync) already use x-ui-client
- Remaining legacy calls are for less frequently used admin operations
