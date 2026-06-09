FROM nginx:1.27-alpine

RUN apk add --no-cache apache2-utils

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
