# s16 新增：独立构建固定 ctags；查询不会自动执行这些下载/安装步骤。
FROM alpine:3.23 AS ctags
RUN apk add --no-cache git autoconf automake pkgconf gcc make musl-dev jansson-dev
WORKDIR /ctags
RUN git init && git remote add origin https://github.com/universal-ctags/ctags.git \
    && git fetch --depth=1 origin refs/tags/v6.1.0 && git checkout --detach FETCH_HEAD \
    && ./autogen.sh \
    && ./configure --enable-json --disable-xml --disable-yaml --disable-pcre2 --disable-seccomp \
    && make -j2 \
    && ./ctags --list-features | grep -q json

# s16 新增：固定同一 Zoekt 提交，只替换薄查询适配器。
FROM golang:1.26.5-alpine AS build
RUN apk add --no-cache git
WORKDIR /zoekt
RUN git init && git remote add origin https://github.com/sourcegraph/zoekt.git \
    && git fetch --depth=1 origin 27517a30ae6df16c6e768623847fdab8f6a3bbcf \
    && git checkout --detach FETCH_HEAD
COPY search.go cmd/zero2pi-search/main.go
RUN CGO_ENABLED=0 go build -trimpath -o /out/zoekt-index ./cmd/zoekt-index \
    && CGO_ENABLED=0 go build -trimpath -o /out/search ./cmd/zero2pi-search

# s16 新增：ctags 需要 musl/jansson，运行镜像保留其实际依赖。
FROM alpine:3.23
RUN apk add --no-cache jansson
LABEL zero2pi.zoekt.commit="27517a30ae6df16c6e768623847fdab8f6a3bbcf"
LABEL zero2pi.zoekt.adapter="s16-v2"
LABEL zero2pi.ctags.version="6.1.0"
COPY --from=ctags /ctags/ctags /usr/local/bin/universal-ctags
COPY --from=ctags /ctags/COPYING /licenses/ctags-COPYING
COPY --from=build /out/zoekt-index /zoekt-index
COPY --from=build /out/search /search
COPY --from=build /zoekt/LICENSE /licenses/zoekt-LICENSE
ENTRYPOINT ["/search"]
