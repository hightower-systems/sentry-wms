FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# v1.4.2 #73: bake the source version into the image so the app can detect
# an upgrade-without-rebuild at startup (code pulled but image not rebuilt
# leaves stale dependencies -- e.g., flask_limiter missing after v1.3->v1.4).
RUN grep -oP '(?<=__version__ = ")[^"]+' /app/version.py > /app/BUILD_VERSION

RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 5000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:create_app()"]
