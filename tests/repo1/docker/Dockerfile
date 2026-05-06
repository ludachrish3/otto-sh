FROM alpine:3.20
RUN apk add --no-cache bash
COPY marker.txt /etc/repo1-marker.txt
CMD ["sh", "-c", "while sleep 3600; do :; done"]
