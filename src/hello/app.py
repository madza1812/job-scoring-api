import json

def handler(event, context):
    # event = the HTTP request (path, headers, body) from API Gateway
    # context = runtime info, e.g. a unique request ID
    return {        
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({            
            "message": "hello from Lambda",
            "version": "v2",
            "request_id": context.aws_request_id,
        }),
    }