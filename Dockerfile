FROM python:3.10-slim

# --- Java 21 (Minecraft sunucuları için) ---
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L -o /tmp/jre21.tar.gz \
      "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.1%2B12/OpenJDK21U-jre_x64_linux_hotspot_21.0.1_12.tar.gz" \
    && mkdir -p /opt/java \
    && tar -xzf /tmp/jre21.tar.gz -C /opt/java --strip-components=1 \
    && rm /tmp/jre21.tar.gz
ENV JAVA_HOME=/opt/java
ENV PATH=/opt/java/bin:$PATH

WORKDIR /app

# --- Python bağımlılıkları ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Node agent ---
COPY sistem.py .

# Node API + Minecraft portları
EXPOSE 7860
ENV PORT=7860

# Loglar anında görünsün
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "sistem.py"]
