@echo off
if exist .venv\Scripts\activate.bat (
    echo Activating Virtual Environment...
    call .venv\Scripts\activate.bat
) else (
    echo No .venv found. Proceeding with global Python environment.
)

echo Installing PyInstaller...
pip install pyinstaller

echo.
echo Building WWKS2 ADS Gateway...
pyinstaller --name wwks2-ads-gateway ^
            --onefile ^
            --clean ^
            --collect-all pyads ^
            --collect-all fastapi ^
            --collect-all uvicorn ^
            --collect-all websockets ^
            service.py

echo.
echo Build complete! The executable is located in the "dist" folder.
echo Make sure "config.toml" is placed in the same directory as the executable.
pause
