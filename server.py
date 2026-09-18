from fastmcp import FastMCP, Context
from fastmcp.server.auth.providers.google import GoogleProvider
from typing import Any, Dict, List, Optional
import os
import logging
import requests
import json

# Load environment variables FIRST
from dotenv import load_dotenv
load_dotenv()

# Import OAuth modules after environment is loaded
from oauth.google_auth import get_headers_with_auto_token

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('google_analytics_server')

# -----------------------------------------------------------------------------
# OAuth configuration (HTTP transport only)
# -----------------------------------------------------------------------------
# GoogleProvider runs a full OAuth proxy in front of the MCP endpoint:
# it exposes /authorize, /token, /register and a callback (default
# "/auth/callback") relative to SERVER_BASE_URL, drives the Google consent
# screen, and keeps the resulting Google access token refreshed for the
# lifetime of the session. None of the three values below are optional for
# a public HTTP deployment - without them the server would accept requests
# from anyone who finds the URL.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL")

# Default GA4 property to use when a tool call doesn't specify property_id.
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "")

HTTP_MODE = "--stdio" not in __import__("sys").argv

auth_provider = None
if HTTP_MODE:
    missing = [
        name for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", GOOGLE_OAUTH_CLIENT_ID),
            ("GOOGLE_OAUTH_CLIENT_SECRET", GOOGLE_OAUTH_CLIENT_SECRET),
            ("SERVER_BASE_URL", SERVER_BASE_URL),
        ) if not value
    ]
    if missing and os.environ.get("ALLOW_UNAUTHENTICATED") != "true":
        raise SystemExit(
            "Refusing to start HTTP transport without OAuth configured. "
            f"Missing environment variable(s): {', '.join(missing)}. "
            "Set ALLOW_UNAUTHENTICATED=true only for local, non-public testing."
        )
    if not missing:
        auth_provider = GoogleProvider(
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
            base_url=SERVER_BASE_URL,
            required_scopes=[
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/analytics.readonly",
            ],
        )
        logger.info(f"OAuth configured via GoogleProvider (base_url={SERVER_BASE_URL})")
    else:
        logger.warning(
            "Starting HTTP transport WITHOUT authentication (ALLOW_UNAUTHENTICATED=true). "
            "Anyone with the URL can query this Google Analytics data."
        )

mcp = FastMCP("Google Analytics Tools", auth=auth_provider)

# Server startup
logger.info("Starting Google Analytics MCP Server...")


def _resolve_property_id(property_id: Optional[str]) -> str:
    """Fall back to the GA4_PROPERTY_ID env var when a tool call omits it."""
    resolved = property_id or GA4_PROPERTY_ID
    if not resolved:
        raise ValueError(
            "property_id is required (pass it explicitly, or set the "
            "GA4_PROPERTY_ID environment variable as a default)."
        )
    return resolved

@mcp.tool
def list_properties(
    account_id: str = "",
    ctx: Context = None
) -> Dict[str, Any]:
    """List all Google Analytics 4 accounts with their associated properties in a hierarchical structure.
    
    Args:
        account_id: Optional specific Google Analytics account ID to list properties for.
                   If not provided, will list all accessible accounts with their properties.
    
    Returns:
        Hierarchical structure showing Account ID/Name with all associated Property IDs/Names
    """
    if ctx:
        if account_id:
            ctx.info(f"Listing properties for account {account_id}...")
        else:
            ctx.info("Listing all accessible Google Analytics accounts and properties...")

    try:
        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        accounts_with_properties = []

        if account_id:
            # Get specific account info - try v1 then v1beta
            account_url = f"https://analyticsadmin.googleapis.com/v1/accounts/{account_id}"
            
            account_response = requests.get(account_url, headers=headers)
            api_version = 'v1'

            # Try v1beta if v1 fails
            if not account_response.ok:
                account_url = f"https://analyticsadmin.googleapis.com/v1beta/accounts/{account_id}"
                account_response = requests.get(account_url, headers=headers)
                api_version = 'v1beta'

            if not account_response.ok:
                if ctx:
                    ctx.error(f"Failed to get account {account_id}: {account_response.status_code} {account_response.reason}")
                raise Exception(f"Admin API error: {account_response.status_code} {account_response.reason} - {account_response.text}")

            account = account_response.json()
            account_name = account.get('name', '')  # Format: accounts/297364605

            # Get properties for this account
            properties_url = f"https://analyticsadmin.googleapis.com/{api_version}/{account_name}/properties"
            
            properties = []
            try:
                properties_response = requests.get(properties_url, headers=headers)

                if properties_response.ok:
                    properties_results = properties_response.json()
                    properties = properties_results.get('properties', [])
                else:
                    # Try alternative format
                    alt_properties_url = f"https://analyticsadmin.googleapis.com/{api_version}/properties?filter=parent:{account_name}"
                    alt_response = requests.get(alt_properties_url, headers=headers)
                    
                    if alt_response.ok:
                        alt_results = alt_response.json()
                        properties = alt_results.get('properties', [])
                    else:
                        if ctx:
                            ctx.error(f"Failed to get properties: {properties_response.status_code} {properties_response.reason}")
                        raise Exception(f"Admin API error: {properties_response.status_code} {properties_response.reason} - {properties_response.text}")
            except Exception as property_error:
                if ctx:
                    ctx.error(f"Error fetching properties for account {account_id}: {str(property_error)}")
                raise

            accounts_with_properties.append({
                'accountId': account_id,
                'accountName': account.get('displayName', 'Unnamed Account'),
                'accountCreateTime': account.get('createTime', 'Unknown'),
                'propertyCount': len(properties),
                'apiVersion': api_version,
                'properties': [
                    {
                        'propertyId': prop.get('name', '').split('/')[-1] if prop.get('name') else 'Unknown',
                        'displayName': prop.get('displayName', 'Unnamed Property'),
                        'propertyType': prop.get('propertyType', 'PROPERTY_TYPE_UNSPECIFIED'),
                        'timeZone': prop.get('timeZone', 'Unknown'),
                        'currencyCode': prop.get('currencyCode', 'Unknown'),
                        'industryCategory': prop.get('industryCategory', 'Unknown'),
                        'createTime': prop.get('createTime', 'Unknown')
                    }
                    for prop in properties
                ]
            })

        else:
            # Get all accounts
            accounts_url = 'https://analyticsadmin.googleapis.com/v1/accounts'
            
            accounts_response = requests.get(accounts_url, headers=headers)
            api_version = 'v1'
            
            if not accounts_response.ok:
                accounts_url = 'https://analyticsadmin.googleapis.com/v1beta/accounts'
                accounts_response = requests.get(accounts_url, headers=headers)
                api_version = 'v1beta'

            if not accounts_response.ok:
                if ctx:
                    ctx.error(f"Failed to list accounts: {accounts_response.status_code} {accounts_response.reason}")
                raise Exception(f"Admin API error: {accounts_response.status_code} {accounts_response.reason} - {accounts_response.text}")

            accounts_results = accounts_response.json()
            accounts = accounts_results.get('accounts', [])

            # Get properties for each account
            for account in accounts:
                account_name = account.get('name', '')  # Format: accounts/123456789
                account_id_extracted = account_name.split('/')[-1] if account_name else 'Unknown'
                
                properties_url = f"https://analyticsadmin.googleapis.com/{api_version}/{account_name}/properties"
                
                properties = []
                try:
                    properties_response = requests.get(properties_url, headers=headers)

                    if properties_response.ok:
                        properties_results = properties_response.json()
                        properties = properties_results.get('properties', [])
                    else:
                        # Try alternative format
                        alt_properties_url = f"https://analyticsadmin.googleapis.com/{api_version}/properties?filter=parent:{account_name}"
                        alt_response = requests.get(alt_properties_url, headers=headers)
                        
                        if alt_response.ok:
                            alt_results = alt_response.json()
                            properties = alt_results.get('properties', [])
                except Exception as property_error:
                    if ctx:
                        ctx.warning(f"Error fetching properties for account {account_name}: {str(property_error)}")

                accounts_with_properties.append({
                    'accountId': account_id_extracted,
                    'accountName': account.get('displayName', 'Unnamed Account'),
                    'accountCreateTime': account.get('createTime', 'Unknown'),
                    'propertyCount': len(properties),
                    'apiVersion': api_version,
                    'properties': [
                        {
                            'propertyId': prop.get('name', '').split('/')[-1] if prop.get('name') else 'Unknown',
                            'displayName': prop.get('displayName', 'Unnamed Property'),
                            'propertyType': prop.get('propertyType', 'PROPERTY_TYPE_UNSPECIFIED'),
                            'timeZone': prop.get('timeZone', 'Unknown'),
                            'currencyCode': prop.get('currencyCode', 'Unknown'),
                            'industryCategory': prop.get('industryCategory', 'Unknown'),
                            'createTime': prop.get('createTime', 'Unknown')
                        }
                        for prop in properties
                    ]
                })

        total_accounts = len(accounts_with_properties)
        total_properties = sum(account['propertyCount'] for account in accounts_with_properties)

        if total_accounts == 0:
            message = f"No account found with ID {account_id}" if account_id else "No accounts found or no access to any accounts"
            if ctx:
                ctx.info(message)
            return {
                'message': message,
                'summary': {
                    'totalAccounts': 0,
                    'totalProperties': 0
                },
                'accounts': []
            }

        if ctx:
            ctx.info(f"Found {total_accounts} accounts with {total_properties} total properties.")

        return {
            'summary': {
                'totalAccounts': total_accounts,
                'totalProperties': total_properties,
                'queriedAccountId': account_id if account_id else None
            },
            'accounts': accounts_with_properties
        }

    except Exception as e:
        if ctx:
            ctx.error(f"Error listing properties: {str(e)}")
        raise

@mcp.tool
def get_page_views(
    start_date: str,
    end_date: str,
    property_id: str = "",
    dimensions: Optional[List[str]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """Get page view metrics for a specific date range from Google Analytics 4.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        property_id: Google Analytics 4 property ID (numeric, e.g., "123456789").
                     Optional if the GA4_PROPERTY_ID environment variable is set.
        dimensions: List of dimensions to group by (optional, defaults to ["pagePath"])

    Returns:
        Page view metrics grouped by specified dimensions in JSON format
    """
    property_id = _resolve_property_id(property_id)

    if ctx:
        ctx.info(f"Getting page views for property {property_id} from {start_date} to {end_date}...")

    try:
        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

        # Build payload
        payload = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'metrics': [{'name': 'screenPageViews'}]
        }

        # Add dimensions if provided
        if dimensions and len(dimensions) > 0:
            payload['dimensions'] = [{'name': dim} for dim in dimensions]
        else:
            # Default to pagePath if no dimensions specified
            payload['dimensions'] = [{'name': 'pagePath'}]

        response = requests.post(url, headers=headers, json=payload)

        if not response.ok:
            if ctx:
                ctx.error(f"Google Analytics API error: {response.status_code} {response.reason}")
            raise Exception(f"Google Analytics API error: {response.status_code} {response.reason} - {response.text}")

        results = response.json()

        # Check if no results found
        if not results.get('rows') or len(results.get('rows', [])) == 0:
            message = f"No page view data found for property {property_id} from {start_date} to {end_date}"
            if ctx:
                ctx.info(message)
            return {'message': message}

        if ctx:
            ctx.info(f"Found {len(results.get('rows', []))} rows of page view data.")

        return results

    except Exception as e:
        if ctx:
            ctx.error(f"Error getting page views: {str(e)}")
        raise

@mcp.tool
def get_active_users(
    start_date: str,
    end_date: str,
    property_id: str = "",
    dimensions: Optional[List[str]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """Get active users metrics for a specific date range from Google Analytics 4.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        property_id: Google Analytics 4 property ID (numeric, e.g., "123456789").
                     Optional if the GA4_PROPERTY_ID environment variable is set.
        dimensions: List of dimensions to group by (optional, defaults to ["date"])

    Returns:
        Active users metrics grouped by specified dimensions in JSON format
    """
    property_id = _resolve_property_id(property_id)

    if ctx:
        ctx.info(f"Getting active users for property {property_id} from {start_date} to {end_date}...")

    try:
        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

        # Build payload
        payload = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'metrics': [{'name': 'activeUsers'}]
        }

        # Add dimensions if provided
        if dimensions and len(dimensions) > 0:
            payload['dimensions'] = [{'name': dim} for dim in dimensions]
        else:
            # Default to date if no dimensions specified
            payload['dimensions'] = [{'name': 'date'}]

        response = requests.post(url, headers=headers, json=payload)

        if not response.ok:
            if ctx:
                ctx.error(f"Google Analytics API error: {response.status_code} {response.reason}")
            raise Exception(f"Google Analytics API error: {response.status_code} {response.reason} - {response.text}")

        results = response.json()

        # Check if no results found
        if not results.get('rows') or len(results.get('rows', [])) == 0:
            message = f"No active users data found for property {property_id} from {start_date} to {end_date}"
            if ctx:
                ctx.info(message)
            return {'message': message}

        if ctx:
            ctx.info(f"Found {len(results.get('rows', []))} rows of active users data.")

        return results

    except Exception as e:
        if ctx:
            ctx.error(f"Error getting active users: {str(e)}")
        raise

@mcp.tool
def get_events(
    start_date: str,
    end_date: str,
    property_id: str = "",
    dimensions: Optional[List[str]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """Get event metrics for a specific date range from Google Analytics 4.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        property_id: Google Analytics 4 property ID (numeric, e.g., "123456789").
                     Optional if the GA4_PROPERTY_ID environment variable is set.
        dimensions: List of dimensions to group by (optional, defaults to ["eventName"])

    Returns:
        Event metrics grouped by specified dimensions in JSON format
    """
    property_id = _resolve_property_id(property_id)

    if ctx:
        ctx.info(f"Getting events for property {property_id} from {start_date} to {end_date}...")

    try:
        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

        # Build payload
        payload = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'metrics': [{'name': 'eventCount'}]
        }

        # Add dimensions if provided
        if dimensions and len(dimensions) > 0:
            payload['dimensions'] = [{'name': dim} for dim in dimensions]
        else:
            # Default to eventName if no dimensions specified
            payload['dimensions'] = [{'name': 'eventName'}]

        response = requests.post(url, headers=headers, json=payload)

        if not response.ok:
            if ctx:
                ctx.error(f"Google Analytics API error: {response.status_code} {response.reason}")
            raise Exception(f"Google Analytics API error: {response.status_code} {response.reason} - {response.text}")

        results = response.json()

        # Check if no results found
        if not results.get('rows') or len(results.get('rows', [])) == 0:
            message = f"No events data found for property {property_id} from {start_date} to {end_date}"
            if ctx:
                ctx.info(message)
            return {'message': message}

        if ctx:
            ctx.info(f"Found {len(results.get('rows', []))} rows of events data.")

        return results

    except Exception as e:
        if ctx:
            ctx.error(f"Error getting events: {str(e)}")
        raise

@mcp.tool
def get_traffic_sources(
    start_date: str,
    end_date: str,
    property_id: str = "",
    dimensions: Optional[List[str]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """Get traffic source metrics for a specific date range from Google Analytics 4.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        property_id: Google Analytics 4 property ID (numeric, e.g., "123456789").
                     Optional if the GA4_PROPERTY_ID environment variable is set.
        dimensions: List of dimensions to group by (optional, defaults to ["source", "medium"])

    Returns:
        Traffic source metrics grouped by specified dimensions in JSON format
    """
    property_id = _resolve_property_id(property_id)

    if ctx:
        ctx.info(f"Getting traffic sources for property {property_id} from {start_date} to {end_date}...")
    
    try:
        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

        # Build payload
        payload = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'metrics': [{'name': 'sessions'}, {'name': 'totalUsers'}]
        }

        # Add dimensions if provided
        if dimensions and len(dimensions) > 0:
            payload['dimensions'] = [{'name': dim} for dim in dimensions]
        else:
            # Default to source and medium if no dimensions specified
            payload['dimensions'] = [{'name': 'source'}, {'name': 'medium'}]

        response = requests.post(url, headers=headers, json=payload)

        if not response.ok:
            if ctx:
                ctx.error(f"Google Analytics API error: {response.status_code} {response.reason}")
            raise Exception(f"Google Analytics API error: {response.status_code} {response.reason} - {response.text}")

        results = response.json()

        # Check if no results found
        if not results.get('rows') or len(results.get('rows', [])) == 0:
            message = f"No traffic sources data found for property {property_id} from {start_date} to {end_date}"
            if ctx:
                ctx.info(message)
            return {'message': message}

        if ctx:
            ctx.info(f"Found {len(results.get('rows', []))} rows of traffic sources data.")

        return results

    except Exception as e:
        if ctx:
            ctx.error(f"Error getting traffic sources: {str(e)}")
        raise

@mcp.tool
def get_device_metrics(
    start_date: str,
    end_date: str,
    property_id: str = "",
    dimensions: Optional[List[str]] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """Get device metrics for a specific date range from Google Analytics 4.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        property_id: Google Analytics 4 property ID (numeric, e.g., "123456789").
                     Optional if the GA4_PROPERTY_ID environment variable is set.
        dimensions: List of dimensions to group by (optional, defaults to ["deviceCategory"])

    Returns:
        Device metrics grouped by specified dimensions in JSON format
    """
    property_id = _resolve_property_id(property_id)

    if ctx:
        ctx.info(f"Getting device metrics for property {property_id} from {start_date} to {end_date}...")

    try:
        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

        # Build payload
        payload = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'metrics': [{'name': 'sessions'}, {'name': 'screenPageViews'}]
        }

        # Add dimensions if provided
        if dimensions and len(dimensions) > 0:
            payload['dimensions'] = [{'name': dim} for dim in dimensions]
        else:
            # Default to deviceCategory if no dimensions specified
            payload['dimensions'] = [{'name': 'deviceCategory'}]

        response = requests.post(url, headers=headers, json=payload)

        if not response.ok:
            if ctx:
                ctx.error(f"Google Analytics API error: {response.status_code} {response.reason}")
            raise Exception(f"Google Analytics API error: {response.status_code} {response.reason} - {response.text}")

        results = response.json()

        # Check if no results found
        if not results.get('rows') or len(results.get('rows', [])) == 0:
            message = f"No device metrics data found for property {property_id} from {start_date} to {end_date}"
            if ctx:
                ctx.info(message)
            return {'message': message}

        if ctx:
            ctx.info(f"Found {len(results.get('rows', []))} rows of device metrics data.")

        return results

    except Exception as e:
        if ctx:
            ctx.error(f"Error getting device metrics: {str(e)}")
        raise

@mcp.tool
def run_report(
    start_date: str,
    end_date: str,
    metrics: List[str],
    property_id: str = "",
    dimensions: Optional[List[str]] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    order_bys: Optional[List[Dict[str, Any]]] = None,
    dimension_filter: Optional[Dict[str, Any]] = None,
    metric_filter: Optional[Dict[str, Any]] = None,
    keep_empty_rows: Optional[bool] = None,
    ctx: Context = None
) -> Dict[str, Any]:
    """Execute a comprehensive Google Analytics 4 report with full customization capabilities.
    
    IMPORTANT: Use STRING ARRAYS for metrics and dimensions, NOT objects!
    
    CORRECT FORMAT:
    - metrics: ["sessions", "totalUsers", "screenPageViews"]  
    - dimensions: ["country", "deviceCategory"]  
    
    INCORRECT FORMAT (will fail):
    - metrics: [{"name": "sessions"}]  
    - dimensions: [{"name": "country"}]  
    
    VALID GA4 METRICS:
    - sessions, totalUsers, activeUsers, newUsers
    - screenPageViews, pageviews, bounceRate, engagementRate
    - averageSessionDuration, userEngagementDuration, engagedSessions
    - conversions, totalRevenue, purchaseRevenue
    - eventCount, eventsPerSession
    
    COMMON METRIC MISTAKES:
    - uniquePageviews (not valid) → use screenPageViews
    - pageViews (not valid) → use screenPageViews
    - users (not valid) → use totalUsers or activeUsers
    - sessionDuration (not valid) → use averageSessionDuration
    - conversionsPerSession (not valid) → use eventsPerSession
    - conversionRate (not valid) → calculate manually
    
    VALID GA4 DIMENSIONS:
    - country, city, region, continent
    - deviceCategory, operatingSystem, browser
    - source, medium, campaignName, sessionDefaultChannelGroup
    - pagePath, pageTitle, landingPage
    - date, month, year, hour, dayOfWeek
    - sessionSource, sessionMedium, sessionCampaignName
    
    COMMON DIMENSION MISTAKES:
    - channelGroup (not valid) → use sessionDefaultChannelGroup
    - sessionCampaign (not valid) → use sessionCampaignName
    - campaign (not valid) → use campaignName
    
    SORTING (order_bys) - EXPERIMENTAL:
    - For metrics: [{"metric": {"metricName": "sessions"}, "desc": true}]
    - For dimensions: [{"dimension": {"dimensionName": "country"}, "desc": false}]
    - WARNING: Sorting may fail due to JSON parsing issues. Test without sorting first.
    
    Args:
        start_date: Start date in YYYY-MM-DD format (e.g., "2025-01-01")
        end_date: End date in YYYY-MM-DD format (e.g., "2025-01-31")
        metrics: Array of metric names as STRINGS (e.g., ["sessions", "totalUsers"])
        property_id: Google Analytics 4 property ID (numeric, e.g., "421301275").
                     Optional if the GA4_PROPERTY_ID environment variable is set.
        dimensions: Optional array of dimension names as STRINGS (e.g., ["country", "deviceCategory"])
        limit: Optional maximum number of rows (default: 100)
        offset: Optional number of rows to skip (default: 0)
        order_bys: Optional sorting - see format above
        dimension_filter: Optional filter for dimensions
        metric_filter: Optional filter for metrics
        keep_empty_rows: Optional boolean to include empty rows
    
    Returns:
        Comprehensive JSON report with requested metrics and dimensions
    
    WORKING EXAMPLES:
    
    Basic Sessions Report:
    {
      "property_id": "421301275",
      "start_date": "2025-01-01",
      "end_date": "2025-01-31",
      "metrics": ["sessions", "totalUsers", "screenPageViews"]
    }
    
    Traffic by Country:
    {
      "property_id": "421301275", 
      "start_date": "2025-01-01",
      "end_date": "2025-01-31",
      "metrics": ["sessions", "totalUsers"],
      "dimensions": ["country", "deviceCategory"],
      "limit": 20
    }
    
    Top Pages Report:
    {
      "property_id": "421301275",
      "start_date": "2025-01-01", 
      "end_date": "2025-01-31",
      "metrics": ["screenPageViews", "sessions"],
      "dimensions": ["pagePath"],
      "limit": 10
    }
    """
    property_id = _resolve_property_id(property_id)

    if ctx:
        ctx.info(f"Running comprehensive report for property {property_id} from {start_date} to {end_date}...")
        ctx.info(f"Metrics: {', '.join(metrics)}")
        if dimensions:
            ctx.info(f"Dimensions: {', '.join(dimensions)}")

    try:
        # Basic validation only
        if not start_date or not isinstance(start_date, str):
            raise ValueError("start_date is required and must be a string in YYYY-MM-DD format")

        if not end_date or not isinstance(end_date, str):
            raise ValueError("end_date is required and must be a string in YYYY-MM-DD format")

        if not metrics or not isinstance(metrics, list) or len(metrics) == 0:
            raise ValueError("metrics is required and must be a non-empty array")

        # This will automatically trigger OAuth flow if needed
        headers = get_headers_with_auto_token()
        
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

        # Construct the payload
        payload = {
            'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
            'metrics': [{'name': metric.strip()} for metric in metrics]
        }

        # Add optional parameters
        if dimensions and isinstance(dimensions, list) and len(dimensions) > 0:
            payload['dimensions'] = [{'name': dimension.strip()} for dimension in dimensions]

        if limit is not None and isinstance(limit, int) and limit > 0:
            payload['limit'] = limit

        if offset is not None and isinstance(offset, int) and offset >= 0:
            payload['offset'] = offset

        if order_bys and isinstance(order_bys, list) and len(order_bys) > 0:
            payload['orderBys'] = order_bys

        if dimension_filter and isinstance(dimension_filter, dict):
            payload['dimensionFilter'] = dimension_filter

        if metric_filter and isinstance(metric_filter, dict):
            payload['metricFilter'] = metric_filter

        if keep_empty_rows is not None and isinstance(keep_empty_rows, bool):
            payload['keepEmptyRows'] = keep_empty_rows

        response = requests.post(url, headers=headers, json=payload)
        
        if not response.ok:
            if ctx:
                ctx.error(f"Google Analytics API error: {response.status_code} {response.reason}")
            raise Exception(f"Google Analytics API error: {response.status_code} {response.reason} - {response.text}")
        
        results = response.json()
        
        # Check if no results found
        if not results.get('rows') or len(results.get('rows', [])) == 0:
            message = f"No data found for property {property_id} from {start_date} to {end_date}"
            if ctx:
                ctx.info(message)
            return {
                'message': message,
                'property_id': property_id,
                'start_date': start_date,
                'end_date': end_date,
                'metrics_requested': metrics,
                'dimensions_requested': dimensions or [],
                'total_rows': 0
            }

        if ctx:
            ctx.info(f"Report completed successfully. Found {len(results.get('rows', []))} rows of data.")

        # Add metadata to results
        results['metadata'] = {
            'property_id': property_id,
            'start_date': start_date,
            'end_date': end_date,
            'metrics_requested': metrics,
            'dimensions_requested': dimensions or [],
            'total_rows': len(results.get('rows', []))
        }

        return results

    except Exception as e:
        if ctx:
            ctx.error(f"Error running report: {str(e)}")
        raise

@mcp.resource("ga4://reference")
def ga4_reference() -> str:
    """Google Analytics 4 API reference documentation."""
    return """
    ## Google Analytics 4 API Reference
    
    ### Common Metrics
    - sessions: Number of sessions
    - totalUsers: Total number of users
    - activeUsers: Number of active users
    - newUsers: Number of new users
    - screenPageViews: Number of page/screen views
    - bounceRate: Bounce rate percentage
    - engagementRate: Engagement rate percentage
    - averageSessionDuration: Average session duration
    - conversions: Number of conversions
    - totalRevenue: Total revenue
    - eventCount: Number of events
    
    ### Common Dimensions
    - country: Country name
    - city: City name
    - deviceCategory: Device category (mobile, desktop, tablet)
    - source: Traffic source
    - medium: Traffic medium
    - campaignName: Campaign name
    - pagePath: Page path
    - eventName: Event name
    - date: Date (YYYYMMDD format)
    - month: Month
    - year: Year
    
    ### Date Format
    All dates should be in YYYY-MM-DD format (e.g., "2025-01-01")
    
    ### Example API Calls
    
    1. Basic page views:
    get_page_views(property_id="123456789", start_date="2025-01-01", end_date="2025-01-31")
    
    2. Traffic sources:
    get_traffic_sources(property_id="123456789", start_date="2025-01-01", end_date="2025-01-31")
    
    3. Custom report:
    run_report(
        property_id="123456789",
        start_date="2025-01-01", 
        end_date="2025-01-31",
        metrics=["sessions", "totalUsers", "screenPageViews"],
        dimensions=["country", "deviceCategory"]
    )
    """

if __name__ == "__main__":
    import sys

    # HTTP transport is the default (suitable for a Render web service, or
    # any deployment behind a public URL). Pass --stdio for local use with
    # Claude Desktop; that mode never applies the OAuth provider above.
    if "--stdio" in sys.argv:
        logger.info("Starting with STDIO transport for Claude Desktop")
        mcp.run(transport="stdio")
    else:
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8000"))
        logger.info(f"Starting with HTTP transport on http://{host}:{port}/mcp")
        mcp.run(transport="streamable-http", host=host, port=port, path="/mcp")