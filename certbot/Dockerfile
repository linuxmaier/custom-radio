FROM certbot/certbot
# docker-cli needed for the --deploy-hook that reloads nginx after cert renewal
RUN apk add --no-cache docker-cli
COPY reload-nginx.sh /usr/local/bin/reload-nginx
RUN chmod +x /usr/local/bin/reload-nginx
