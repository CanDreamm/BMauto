# Include inside the existing HTTPS server block for bot.itiscat.ru.
# Do not replace the existing site or its TLS certificate settings.
location = /oauth/google/callback {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 5s;
    proxy_read_timeout 45s;
    # OAuth codes must not appear in access or upstream-error logs.
    access_log off;
    error_log /dev/null;
}
