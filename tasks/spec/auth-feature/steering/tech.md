# Tech Stack Steering

## Language & Framework
- Use Python with FastAPI
- Use Pydantic v2 for all request/response models
- Use python-jose for JWT operations
- Use passlib with bcrypt for password hashing

## Code Style
- Always use type hints on all functions
- Use async/await for all route handlers
- Prefer dependency injection via FastAPI Depends()
- Never hardcode secrets — use environment variables

## Testing
- Use pytest with pytest-asyncio
- Use the AAA pattern (Arrange, Act, Assert) in all tests
- Test both happy path and error cases for every endpoint
