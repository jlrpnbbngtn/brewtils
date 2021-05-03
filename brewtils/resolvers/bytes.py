import requests
import six

from brewtils.resolvers.parameter import BYTES_PREFIX, ResolverBase


class BytesResolver(ResolverBase):
    """Resolver that uses new direct BG API"""

    def should_upload(self, value, **_):
        return isinstance(value, six.binary_type)

    def upload(self, value, **_):
        """Upload the bytes value to the server

        Args:
            value: Value to upload.

        Returns:
            A valid beer garden assigned ID
        """
        response = requests.put(url="http://localhost:2337/api/vbeta/file", data=value)

        return response.text

    def should_download(self, value, **_):
        if isinstance(value, six.string_types) and BYTES_PREFIX in value:
            return True
        return False

    def download(self, file_id, **_):
        """Download the given bytes parameter.

        Args:
            file_id: A BG generated bytes ID
        """
        real_id = file_id.partition(BYTES_PREFIX)[2]

        response = requests.get(
            url="http://localhost:2337/api/vbeta/file",
            params={"file_id": real_id},
        )

        return response.content
